from django import forms
from django.template import Context, Template

from finance.templatetags.ui import amount_tone_class, daisy_widget


class _SampleForm(forms.Form):
    name = forms.CharField()
    kind = forms.ChoiceField(choices=(("a", "A"), ("b", "B")))
    notes = forms.CharField(widget=forms.Textarea)
    csv_file = forms.FileField(required=False)


def test_amount_tone_class_uses_sign_or_explicit_tone():
    assert "amount-in" in amount_tone_class("12.00 USD")
    assert "amount-out" in amount_tone_class("-4.00 USD")
    assert "amount-in" not in amount_tone_class("0.00 USD")
    assert "amount-out" in amount_tone_class("12.00 USD", "out")
    assert "amount-in" in amount_tone_class("-4.00 USD", "in")


def test_daisy_widget_adds_control_classes_and_error_state():
    bound = _SampleForm({"name": "", "kind": "a", "notes": "ok"})
    assert "input-error" in daisy_widget(bound["name"])
    assert "select select-bordered" in daisy_widget(bound["kind"])
    assert "textarea" in daisy_widget(bound["notes"])
    errored = _SampleForm({"name": "x", "kind": "a", "notes": ""})
    assert "textarea-error" in daisy_widget(errored["notes"])


def test_daisy_widget_file_fields_use_file_input_class():
    html = daisy_widget(_SampleForm()["csv_file"])
    assert "file-input" in html
    assert "file-input-bordered" in html
    assert "file-input-primary" in html
    errored = _SampleForm(data={})
    errored.add_error("csv_file", "Choose a CSV file.")
    assert "file-input-error" in daisy_widget(errored["csv_file"])


def test_form_include_keeps_field_names():
    form = _SampleForm()
    html = Template("{% include 'finance/_form_fields.html' %}").render(Context({"form": form}))
    assert 'name="name"' in html
    assert 'name="kind"' in html
    assert 'for="' in html
    assert "file-input" in html
    assert 'name="csv_file"' in html
