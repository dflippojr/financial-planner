from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from finance.models import AuditEvent, Household, Membership, Person, SavingsGoal
from finance.savings_goal_import import (
    MAX_FILE_BYTES,
    MAX_ROWS,
    GoalFileError,
    commit_goal_import,
    parse_goal_file,
    preview_goal_import,
)

PASSWORD = "Synthetic-passphrase-42!"
HEADER = "name,target_amount,priority,depends_on,time_sensitive,target_date,scope\n"


def make_person(username):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD)
    return Person.objects.create(user=user, display_name=f"{username.title()} Example")


def make_household(*people):
    household = Household.objects.create(name="Synthetic Household")
    for person in people:
        Membership.objects.create(person=person, household=household)
    return household


def csv_file(*lines, header=HEADER):
    return (header + "\n".join(lines) + "\n").encode()


BASIC = csv_file(
    "Laptop,1200.50,1,,true,2027-03-31,private",
    "Monitor,300,2,Laptop,false,,",
    "Sofa,\"1,800.00\",3,,,,private",
)


def goal_events():
    return AuditEvent.objects.filter(target_type=AuditEvent.TargetType.GOAL)


# -- preview -------------------------------------------------------------------


@pytest.mark.django_db
def test_preview_reports_actions_and_writes_nothing():
    owner = make_person("owner")

    preview = preview_goal_import(owner.user, BASIC)

    assert [row.action for row in preview.rows] == ["create", "create", "create"]
    assert preview.counts.create == 3 and preview.counts.error == 0
    laptop, monitor, sofa = preview.rows
    assert (laptop.target_amount_minor, laptop.time_sensitive, laptop.target_date) == (
        120_050,
        True,
        date(2027, 3, 31),
    )
    assert monitor.dependency is laptop and sofa.target_amount_minor == 180_000
    assert SavingsGoal.objects.count() == 0 and goal_events().count() == 0


@pytest.mark.django_db
def test_amounts_convert_exactly_to_minor_units_and_reject_ambiguous_ones():
    owner = make_person("owner")
    content = csv_file(
        "Exact,$19.90,1,,,,",
        "Zeros,5.000,2,,,,",
        "Three places,1.005,3,,,,",
        "Zero,0,4,,,,",
        "Negative,-5,5,,,,",
        "Words,abc,6,,,,",
        "Not a number,NaN,7,,,,",
        "Huge,99999999999999999999,8,,,,",
    )

    rows = preview_goal_import(owner.user, content).rows

    assert [row.target_amount_minor for row in rows[:2]] == [1_990, 500]
    assert all(row.errors for row in rows[2:])
    assert "two decimal places" in rows[2].errors[0]


@pytest.mark.django_db
def test_row_validation_messages_name_the_row_and_column():
    owner = make_person("owner")
    content = csv_file(
        ",10,1,,,,",
        "Bad priority,10,0,,,,",
        "Bad flag,10,1,,maybe,,",
        "Bad date,10,1,,,31/12/2027,",
        "Bad scope,10,1,,,,family",
        "A" * 151 + ",10,1,,,,",
    )

    errors = [row.errors[0] for row in preview_goal_import(owner.user, content).rows]

    assert errors == [
        "name is required.",
        "priority must be a whole number from 1 to 1000000.",
        "time_sensitive must be true or false.",
        "target_date must look like 2027-03-31.",
        "scope must be private or household.",
        "name can be at most 150 characters.",
    ]


@pytest.mark.django_db
def test_duplicate_names_in_the_file_are_rejected_case_insensitively():
    owner = make_person("owner")

    rows = preview_goal_import(owner.user, csv_file("Laptop,10,1,,,,", "LAPTOP,20,2,,,,")).rows

    assert rows[0].errors == [] and "row 1" in rows[1].errors[0]


@pytest.mark.django_db
def test_unknown_dependency_name_is_rejected_without_naming_anything_else():
    owner = make_person("owner")
    other = make_person("other")
    SavingsGoal.objects.create(owner=other, name="Other secret", target_amount_minor=100)

    for target in ("Nonexistent", "Other secret"):
        rows = preview_goal_import(owner.user, csv_file(f"Laptop,10,1,{target},,,")).rows
        assert rows[0].errors == ["depends_on must be the name of another goal in this file with the same scope."]


@pytest.mark.django_db
def test_dependency_cycle_and_self_dependency_are_rejected():
    owner = make_person("owner")
    cycle = csv_file("A,10,1,B,,,", "B,10,2,C,,,", "C,10,3,A,,,", "D,10,4,D,,,")

    preview = preview_goal_import(owner.user, cycle)

    assert [bool(row.errors) for row in preview.rows] == [True, True, True, True]
    assert "wait on each other" in preview.rows[0].errors[0]
    assert "cannot depend on itself" in preview.rows[3].errors[0]
    with pytest.raises(ValidationError):
        commit_goal_import(owner.user, cycle)
    assert SavingsGoal.objects.count() == 0


@pytest.mark.django_db
def test_cycle_through_a_saved_goal_the_file_does_not_touch_is_rejected():
    owner = make_person("owner")
    commit_goal_import(owner.user, csv_file("A,10,1,,,,", "B,10,2,A,,,"))
    # A depends on B in the file, B (not in the file) already depends on A.
    partial = "name,target_amount,priority,depends_on\nA,10,1,B\n".encode()

    # Column set differs from the first import, so B stays untouched.
    preview = preview_goal_import(owner.user, partial + b"")
    assert preview.rows[0].errors == ["depends_on must be the name of another goal in this file with the same scope."]
    both = "name,target_amount,priority,depends_on\nA,10,1,B\nB,10,2,A\n".encode()
    assert all(row.errors for row in preview_goal_import(owner.user, both).rows)


@pytest.mark.django_db
def test_household_rows_need_a_household_and_never_depend_on_private_rows():
    loner = make_person("loner")
    rows = preview_goal_import(loner.user, csv_file("Sofa,10,1,,,,household")).rows
    assert rows[0].errors == ["Join a household before importing household goals."]

    member = make_person("member")
    make_household(member)
    mixed = csv_file("Mine,10,1,,,,private", "Ours,10,2,Mine,,,household")
    rows = preview_goal_import(member.user, mixed).rows
    assert rows[0].errors == [] and rows[1].errors


@pytest.mark.django_db
def test_file_level_problems_raise_a_safe_message():
    cases = {
        b"": "empty",
        HEADER.encode(): "no goals",
        b"name,priority\nA,1\n": "Missing required column: target_amount.",
        b"name,target_amount,priority,color\nA,1,1,red\n": "Unknown column",
        b"name,name,target_amount,priority\nA,A,1,1\n": "repeats a column",
        b"\xff\xfe\x00": "UTF-8",
        b"[1, 2]": "list of goal objects",
        b'{"goals": "x"}': "list of goal objects",
        b"[{bad json": "JSON could not be read",
        b'[{"name": "A", "target_amount": NaN, "priority": 1}]': "JSON could not be read",
        b'[{"name": "A", "target_amount": 1, "priority": [1]}]': "text, a number",
        b"a" * (MAX_FILE_BYTES + 1): "256 KB",
    }
    for content, expected in cases.items():
        with pytest.raises(GoalFileError) as raised:
            parse_goal_file(content)
        assert expected in str(raised.value)


def test_row_limit_and_extra_cells_are_rejected():
    many = csv_file(*[f"Goal {n},1,{n + 1},,,," for n in range(MAX_ROWS + 1)])
    with pytest.raises(GoalFileError, match="more than"):
        parse_goal_file(many)
    with pytest.raises(GoalFileError, match="more cells"):
        parse_goal_file(b"name,target_amount,priority\nA,1,1,extra\n")


@pytest.mark.django_db
def test_json_input_with_bom_and_wrapped_goals_list_imports():
    owner = make_person("owner")
    content = (
        b'\xef\xbb\xbf{"goals": [{"Name": "Laptop", "target_amount": 1200.5, "priority": 1, '
        b'"time_sensitive": true, "target_date": null}, '
        b'{"name": "Monitor", "target_amount": "300", "priority": "2", "depends_on": "laptop"}]}'
    )

    commit_goal_import(owner.user, content)

    laptop = SavingsGoal.objects.get(name="Laptop")
    monitor = SavingsGoal.objects.get(name="Monitor")
    assert (laptop.target_amount_minor, laptop.time_sensitive, laptop.target_date) == (120_050, True, None)
    assert monitor.depends_on_id == laptop.pk and monitor.priority == 2


# -- commit -----------------------------------------------------------------------


@pytest.mark.django_db
def test_commit_creates_goals_with_dependencies_in_one_pass_and_audits_them():
    owner = make_person("owner")
    # The dependent comes first in the file; the dependency must still be saved first.
    content = csv_file("Monitor,300,2,Laptop,,,", "Laptop,1200,1,,,,")

    result = commit_goal_import(owner.user, content)

    assert result.counts.create == 2
    laptop = SavingsGoal.objects.get(name="Laptop")
    assert SavingsGoal.objects.get(name="Monitor").depends_on_id == laptop.pk
    assert goal_events().filter(action=AuditEvent.Action.RECORD_CREATED).count() == 2
    assert goal_events().filter(action=AuditEvent.Action.RECORD_EDITED).count() == 0


@pytest.mark.django_db
def test_reimporting_the_same_file_changes_nothing_and_audits_nothing():
    owner = make_person("owner")
    commit_goal_import(owner.user, BASIC)
    stamps = {goal.pk: goal.updated_at for goal in SavingsGoal.objects.all()}
    events = goal_events().count()

    second = commit_goal_import(owner.user, BASIC)

    assert (second.counts.create, second.counts.update, second.counts.unchanged) == (0, 0, 3)
    assert SavingsGoal.objects.count() == 3
    assert {goal.pk: goal.updated_at for goal in SavingsGoal.objects.all()} == stamps
    assert goal_events().count() == events


@pytest.mark.django_db
def test_reimport_updates_matching_goals_by_name_ignoring_case_and_lists_missing_ones():
    owner = make_person("owner")
    commit_goal_import(owner.user, BASIC)
    laptop_pk = SavingsGoal.objects.get(name="Laptop").pk
    changed = csv_file(
        "LAPTOP,1500.00,4,,false,2027-06-30,private",
        "Monitor,300,2,Laptop,false,,",
    )

    preview = preview_goal_import(owner.user, changed)
    assert [row.action for row in preview.rows] == ["update", "unchanged"]
    assert preview.rows[0].changes == ["name", "amount", "priority", "time_sensitive", "date"]
    assert [goal.name for goal in preview.missing] == ["Sofa"]

    commit_goal_import(owner.user, changed)

    laptop = SavingsGoal.objects.get(pk=laptop_pk)
    assert (laptop.name, laptop.target_amount_minor, laptop.priority) == ("LAPTOP", 150_000, 4)
    assert (laptop.time_sensitive, laptop.target_date) == (False, date(2027, 6, 30))
    assert SavingsGoal.objects.filter(name="Sofa").exists() and SavingsGoal.objects.count() == 3
    edited = goal_events().get(action=AuditEvent.Action.RECORD_EDITED)
    assert edited.target_id == laptop_pk
    assert set(edited.changed_fields) == {"name", "target", "priority", "time_sensitive", "date"}


@pytest.mark.django_db
def test_columns_left_out_keep_saved_values_and_blank_cells_clear_them():
    owner = make_person("owner")
    commit_goal_import(owner.user, BASIC)
    minimal = b"name,target_amount,priority\nLaptop,1200.50,1\nMonitor,300,2\n"

    assert preview_goal_import(owner.user, minimal).counts.unchanged == 2
    commit_goal_import(owner.user, minimal)
    monitor = SavingsGoal.objects.get(name="Monitor")
    assert monitor.depends_on is not None and SavingsGoal.objects.get(name="Laptop").time_sensitive

    cleared = csv_file("Laptop,1200.50,1,,,,", "Monitor,300,2,,,,")
    commit_goal_import(owner.user, cleared)
    laptop = SavingsGoal.objects.get(name="Laptop")
    monitor.refresh_from_db()
    assert monitor.depends_on is None and laptop.target_date is None and not laptop.time_sensitive


@pytest.mark.django_db
def test_import_matches_only_the_importers_own_goals():
    owner = make_person("owner")
    other = make_person("other")
    theirs = SavingsGoal.objects.create(owner=other, name="Laptop", target_amount_minor=999)

    preview = preview_goal_import(owner.user, csv_file("Laptop,10,1,,,,"))

    assert preview.rows[0].action == "create" and preview.missing == []
    commit_goal_import(owner.user, csv_file("Laptop,10,1,,,,"))
    theirs.refresh_from_db()
    assert theirs.target_amount_minor == 999 and SavingsGoal.objects.filter(name="Laptop").count() == 2


@pytest.mark.django_db
def test_household_goals_are_shared_across_members_on_reimport():
    owner = make_person("owner")
    member = make_person("member")
    household = make_household(owner, member)
    commit_goal_import(owner.user, csv_file("Sofa,500,1,,,,household"))
    sofa = SavingsGoal.objects.get(name="Sofa")
    assert sofa.household_id == household.pk

    commit_goal_import(member.user, csv_file("sofa,650,1,,,,household"))

    sofa.refresh_from_db()
    assert sofa.target_amount_minor == 65_000 and SavingsGoal.objects.count() == 1
    assert sofa.owner_id == owner.pk


@pytest.mark.django_db
def test_ambiguous_saved_names_block_the_row():
    owner = make_person("owner")
    SavingsGoal.objects.create(owner=owner, name="Sofa", target_amount_minor=100)
    SavingsGoal.objects.create(owner=owner, name="sofa", target_amount_minor=200)

    rows = preview_goal_import(owner.user, csv_file("Sofa,10,1,,,,")).rows

    assert "More than one saved goal" in rows[0].errors[0]


@pytest.mark.django_db
def test_commit_refuses_a_file_with_any_error_and_saves_nothing():
    owner = make_person("owner")

    with pytest.raises(ValidationError):
        commit_goal_import(owner.user, csv_file("Good,10,1,,,,", "Bad,abc,2,,,,"))

    assert SavingsGoal.objects.count() == 0


@pytest.mark.django_db
def test_archived_saved_goals_match_but_are_not_listed_as_missing():
    owner = make_person("owner")
    commit_goal_import(owner.user, csv_file("Old,10,1,,,,", "Keep,10,2,,,,"))
    SavingsGoal.objects.filter(name="Old").update(status="archived", archived_at="2026-01-01T00:00:00Z")

    preview = preview_goal_import(owner.user, csv_file("Keep,10,2,,,,"))

    assert preview.missing == []


# -- pages ------------------------------------------------------------------------


def upload(client, content, name="wishlist.csv"):
    return client.post(
        reverse("savings-goal-import"),
        {"goals_file": SimpleUploadedFile(name, content, content_type="text/csv")},
    )


@pytest.mark.django_db
def test_import_page_previews_then_commits_only_when_confirmed(settings, tmp_path):
    settings.CSV_IMPORT_STAGING_DIR = tmp_path
    owner = make_person("owner")
    client = Client()
    client.force_login(owner.user)

    assert client.get(reverse("savings-goal-import")).status_code == 200
    page = upload(client, BASIC)
    token = page.context["token"]

    assert page.status_code == 200 and "Preview" in page.content.decode()
    assert SavingsGoal.objects.count() == 0
    done = client.post(reverse("savings-goal-import"), {"action": "commit", "token": token})
    assert done.status_code == 302 and SavingsGoal.objects.count() == 3
    assert list(tmp_path.iterdir()) == []
    stale = client.post(reverse("savings-goal-import"), {"action": "commit", "token": token})
    assert stale.status_code == 302 and SavingsGoal.objects.count() == 3


@pytest.mark.django_db
def test_import_page_shows_row_errors_and_hides_the_commit_button(settings, tmp_path):
    settings.CSV_IMPORT_STAGING_DIR = tmp_path
    owner = make_person("owner")
    client = Client()
    client.force_login(owner.user)

    page = upload(client, csv_file("Laptop,10,1,Nowhere,,,"))
    body = page.content.decode()

    assert "depends_on must be the name of another goal" in body
    assert 'value="commit"' not in body
    blocked = client.post(
        reverse("savings-goal-import"), {"action": "commit", "token": page.context["token"]}
    )
    assert blocked.status_code == 200 and SavingsGoal.objects.count() == 0


@pytest.mark.django_db
def test_import_page_reports_unreadable_files_and_missing_uploads(settings, tmp_path):
    settings.CSV_IMPORT_STAGING_DIR = tmp_path
    owner = make_person("owner")
    client = Client()
    client.force_login(owner.user)

    unreadable = upload(client, b"name,priority\nA,1\n")
    assert "Missing required column" in unreadable.content.decode()
    assert list(tmp_path.iterdir()) == []
    assert client.post(reverse("savings-goal-import"), {}).status_code == 200


@pytest.mark.django_db
def test_cancel_discards_the_staged_file(settings, tmp_path):
    settings.CSV_IMPORT_STAGING_DIR = tmp_path
    owner = make_person("owner")
    client = Client()
    client.force_login(owner.user)
    token = upload(client, BASIC).context["token"]

    cancelled = client.post(reverse("savings-goal-import"), {"action": "cancel", "token": token})

    assert cancelled.status_code == 302 and list(tmp_path.iterdir()) == []


@pytest.mark.django_db
def test_another_member_cannot_commit_someone_elses_staged_file(settings, tmp_path):
    settings.CSV_IMPORT_STAGING_DIR = tmp_path
    owner = make_person("owner")
    other = make_person("other")
    owner_client = Client()
    owner_client.force_login(owner.user)
    other_client = Client()
    other_client.force_login(other.user)
    token = upload(owner_client, BASIC).context["token"]

    response = other_client.post(reverse("savings-goal-import"), {"action": "commit", "token": token})

    assert response.status_code == 302 and SavingsGoal.objects.count() == 0


@pytest.mark.django_db
def test_commit_after_data_changed_shows_the_current_preview(settings, tmp_path):
    settings.CSV_IMPORT_STAGING_DIR = tmp_path
    owner = make_person("owner")
    client = Client()
    client.force_login(owner.user)
    SavingsGoal.objects.create(owner=owner, name="Laptop", target_amount_minor=1)
    page = upload(client, csv_file("Laptop,10,1,,,,"))
    SavingsGoal.objects.create(owner=owner, name="laptop", target_amount_minor=2)

    again = client.post(reverse("savings-goal-import"), {"action": "commit", "token": page.context["token"]})

    assert again.status_code == 200 and "More than one saved goal" in again.content.decode()


@pytest.mark.django_db
def test_dependency_on_a_row_with_errors_is_reported_on_the_dependent_row():
    owner = make_person("owner")

    rows = preview_goal_import(owner.user, csv_file("Base,abc,1,,,,", "Child,10,2,Base,,,")).rows

    assert "points at row 1" in rows[1].errors[0]


@pytest.mark.django_db
def test_amount_range_limits():
    owner = make_person("owner")
    too_big = "92233720368547759"  # one more than the largest minor-unit total after x100
    rows = preview_goal_import(owner.user, csv_file(f"Big,{too_big},1,,,,", f"Long,{'1' * 41},2,,,,")).rows
    assert all("outside the supported range" in row.errors[0] or "greater than zero" in row.errors[0] for row in rows)


def test_oversized_csv_cell_is_a_file_error():
    with pytest.raises(GoalFileError, match="CSV could not be read"):
        parse_goal_file(b"name,target_amount,priority\n" + b"A" * 140_000 + b",1,1\n")


@pytest.mark.django_db
def test_oversized_upload_is_rejected_on_the_page(settings, tmp_path):
    settings.CSV_IMPORT_STAGING_DIR = tmp_path
    owner = make_person("owner")
    client = Client()
    client.force_login(owner.user)

    page = upload(client, b"a" * (5 * 1024 * 1024 + 1))

    assert page.status_code == 200 and "5 MB" in page.content.decode()
    assert SavingsGoal.objects.count() == 0


@pytest.mark.django_db
def test_active_goal_wins_over_an_archived_one_with_the_same_name():
    owner = make_person("owner")
    old = SavingsGoal.objects.create(
        owner=owner, name="Laptop", target_amount_minor=1, status="archived", archived_at="2026-01-01T00:00:00Z"
    )
    current = SavingsGoal.objects.create(owner=owner, name="laptop", target_amount_minor=2)

    commit_goal_import(owner.user, csv_file("Laptop,10,1,,,,"))

    old.refresh_from_db()
    current.refresh_from_db()
    assert old.target_amount_minor == 1 and current.target_amount_minor == 1000 and SavingsGoal.objects.count() == 2


@pytest.mark.django_db
def test_a_lone_archived_match_is_updated_and_stays_archived():
    owner = make_person("owner")
    old = SavingsGoal.objects.create(
        owner=owner, name="Laptop", target_amount_minor=1, status="archived", archived_at="2026-01-01T00:00:00Z"
    )

    commit_goal_import(owner.user, csv_file("Laptop,10,1,,,,"))

    old.refresh_from_db()
    assert old.target_amount_minor == 1000 and old.status == "archived" and SavingsGoal.objects.count() == 1


def test_deeply_nested_json_is_a_file_error_not_a_crash():
    with pytest.raises(GoalFileError, match="JSON could not be read"):
        parse_goal_file(b"[" * 100_000)


@pytest.mark.django_db
def test_json_values_are_trimmed_and_colliding_keys_are_rejected():
    owner = make_person("owner")
    content = (
        b'[{"name": " Laptop ", "target_amount": " 10 ", "priority": 1}, '
        b'{"name": "Monitor", "target_amount": 5, "priority": 2, "depends_on": " laptop"}]'
    )

    commit_goal_import(owner.user, content)

    assert SavingsGoal.objects.get(name="Monitor").depends_on.name == "Laptop"
    with pytest.raises(GoalFileError, match="repeats a column"):
        parse_goal_file(b'[{"Name": "A", "name": "B", "target_amount": 1, "priority": 1}]')


@pytest.mark.django_db
def test_second_member_confirming_the_same_household_file_changes_nothing():
    owner = make_person("owner")
    member = make_person("member")
    make_household(owner, member)
    content = csv_file("Sofa,500,1,,,,household")

    commit_goal_import(owner.user, content)
    again = commit_goal_import(member.user, content)

    assert again.counts.unchanged == 1 and SavingsGoal.objects.count() == 1
