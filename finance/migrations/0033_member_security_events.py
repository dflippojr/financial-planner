from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ("finance", "0032_monthly_review_ai"),
    ]

    operations = [
        migrations.CreateModel(
            name="MemberSecurityEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "event_type",
                    models.CharField(
                        choices=[
                            ("sign_in_success", "Signed in"),
                            ("sign_in_failure", "Sign-in failed"),
                            ("sign_out", "Signed out"),
                            ("recovery_code_used", "Recovery code used"),
                            ("passkey_added", "Passkey added"),
                            ("passkey_removed", "Passkey removed"),
                            ("password_changed", "Password changed"),
                            ("ai_connection_changed", "AI connection changed"),
                            ("simplefin_connection_changed", "SimpleFIN connection changed"),
                            ("member_data_export", "Data exported"),
                            ("policy_acceptance", "Policy accepted"),
                        ],
                        max_length=40,
                    ),
                ),
                ("occurred_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("ip_address", models.CharField(blank=True, default="", max_length=45)),
                ("user_agent", models.CharField(blank=True, default="", max_length=200)),
                (
                    "member",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="security_events",
                        to="finance.person",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="MemberSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("session_key", models.CharField(max_length=40, unique=True)),
                ("ip_address", models.CharField(blank=True, default="", max_length=45)),
                ("user_agent", models.CharField(blank=True, default="", max_length=200)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("last_activity_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "member",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="member_sessions",
                        to="finance.person",
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="membersecurityevent",
            index=models.Index(fields=["member", "-occurred_at"], name="sec_event_member_occurred_idx"),
        ),
        migrations.AddConstraint(
            model_name="membersecurityevent",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    event_type__in=(
                        "sign_in_success",
                        "sign_in_failure",
                        "sign_out",
                        "recovery_code_used",
                        "passkey_added",
                        "passkey_removed",
                        "password_changed",
                        "ai_connection_changed",
                        "simplefin_connection_changed",
                        "member_data_export",
                        "policy_acceptance",
                    )
                ),
                name="member_security_event_type_valid",
            ),
        ),
        migrations.AddIndex(
            model_name="membersession",
            index=models.Index(fields=["member", "-last_activity_at"], name="member_session_activity_idx"),
        ),
    ]
