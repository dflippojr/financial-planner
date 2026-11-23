"""AI provider connections, defaults and offers."""


from django import forms

from ..ai.api import MODELS as API_MODELS, label as api_label
from ..ai.types import API_KINDS




class HarnessConnectForm(forms.Form):
    base_url = forms.CharField(
        label="Agent Harness Server URL",
        max_length=255,
        widget=forms.URLInput(attrs={"autocomplete": "off"}),
    )
    token = forms.CharField(
        label="App token",
        widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
    )



class ApiKeyConnectForm(forms.Form):
    provider = forms.ChoiceField(label="Provider", choices=[(kind, api_label(kind)) for kind in API_KINDS])  # pyright: ignore[reportArgumentType] - ai_api.label's inferred type includes None; choice labels are always strings
    key = forms.CharField(
        label="API key",
        widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
    )



class ApiKeyDefaultsForm(forms.Form):
    chat_model = forms.ChoiceField(label="Chat model")
    background_model = forms.ChoiceField(label="Background jobs model")
    use_for_chat = forms.BooleanField(required=False, label="Use for chat")
    use_for_background = forms.BooleanField(required=False, label="Use for background jobs")

    def __init__(self, *args, kind, **kwargs):
        super().__init__(*args, **kwargs)
        choices = [(name, name) for name in API_MODELS[kind]]
        self.fields["chat_model"].choices = choices
        self.fields["background_model"].choices = choices



class AiDefaultsForm(forms.Form):
    chat_backend = forms.ChoiceField(label="Chat backend")
    background_backend = forms.ChoiceField(label="Background jobs backend")
    chat_model = forms.CharField(label="Chat model", required=False, max_length=80)
    background_model = forms.CharField(label="Background model", required=False, max_length=80)

    def __init__(self, *args, backends=(), offer_shared_local=False, allow_local_chat=False, **kwargs):
        super().__init__(*args, **kwargs)
        chat_choices = [
            (item.id, item.label)
            for item in backends
            if item.suits_live and (allow_local_chat or item.id != "local")
        ]
        background_choices = [(item.id, item.label) for item in backends if item.suits_background]
        if offer_shared_local:
            chat_choices.append(("shared_local", "Local model (shared)"))
            background_choices.append(("shared_local", "Local model (shared)"))
        if not chat_choices:
            chat_choices = [("", "No available backend")]
        if not background_choices:
            background_choices = [("", "No available backend")]
        self.fields["chat_backend"].choices = chat_choices
        self.fields["background_backend"].choices = background_choices



class AiOfferLocalForm(forms.Form):
    offer_local_to_household = forms.BooleanField(
        required=False,
        label="Offer the local model to household members",
    )



class AiOfferLocalChatForm(forms.Form):
    offer_local_chat = forms.BooleanField(
        required=False,
        label="Offer the local model for chat",
    )



class AiOfferPlanLinksForm(forms.Form):
    offer_plan_links = forms.BooleanField(
        required=False,
        label="Let household members link their own Claude or Codex plan through this connection",
    )



class AiSharedLocalForm(forms.Form):
    use_shared_local_chat = forms.BooleanField(
        required=False,
        label="Chat",
    )
    use_shared_local_background = forms.BooleanField(
        required=False,
        label="Background jobs",
    )
