# Sign in with ChatGPT as a per-member AI backend: research

Status: research only (issue #92). No integration, dependency, key, or sign-up was made. Adopting this as a #91 backend needs a later owner decision and a new issue. No personal financial data appears here.

Source keys map to the URLs below. Sources were accessed on **2026-10-02**. In-body citations use source keys. Anything not stated on a fetched primary page is marked **Unverified**. Secondary blogs were not used as facts.

This would be another backend of the provider layer (#91): each member would connect their own ChatGPT Plus or Pro plan for inference, billed to that plan, not to a household API key.

## Primary sources

- [Q] Quickstart: https://developers.openai.com/siwc/quickstart
- [OSS] Plan usage overview (open-source): https://developers.openai.com/siwc/token-sharing-open-source
- [REG] Registration and sign-in: https://developers.openai.com/siwc/token-sharing-open-source/sign-in
- [SESS] Accounts and sessions: https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions
- [TOK] Token reference: https://developers.openai.com/siwc/token-sharing-open-source/token-reference
- [ERR] Errors and recovery: https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery
- [INF] Models and inference: https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference
- [LIM] Preview limitations: https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations
- [VM] Self-hosted VMs: https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms
- [WEB] On your website (identity): https://developers.openai.com/siwc/website
- [UX] UI/UX guidelines: https://developers.openai.com/siwc/ui-ux-guidelines
- [CB] Cookbook (28 Sep 2026): https://developers.openai.com/cookbook/articles/sign-in-with-chatgpt
- [DD] DevDay 2026: https://learn.chatgpt.com/docs/whats-new/devday-2026
- [LEARN] User guide: https://learn.chatgpt.com/docs/sign-in-with-chatgpt
- [TERMS] Sign in with ChatGPT Terms (29 Sep 2026): https://openai.com/policies/sign-in-with-chatgpt-terms/

Fetched pages that timed out on 2026-10-02 (do not treat as read): `openai.com/policies/service-terms/`, `openai.com/policies/how-your-data-is-used-to-improve-model-performance/`, `help.openai.com/en/articles/7039943-how-openai-handles-data-in-consumer-services`, `help.openai.com/en/articles/6950777-what-is-chatgpt-plus`, `help.openai.com/en/articles/20001542-using-your-chatgpt-plan-in-other-apps-and-sites`, `help.openai.com/en/articles/20001410-sign-in-with-chatgpt`, `openai.com/enterprise-privacy/`, `platform.openai.com/docs/guides/your-data`. Facts that would need those pages remain Unverified.

## Eligibility

| Category in OpenAI docs | What the pages say | Fit for this app |
|---|---|---|
| Open-source / locally running personal tools | Self-serve ChatGPT **plan usage** for OSS and locally hosted apps, via dynamic registration (`client_id=dynamic_agent_client`), no client secret or partner API key [Q] [OSS] [REG] [CB] | The GitHub repo is public and open-source, which matches the OSS product. The deployed app is a **multi-user household server** reached at a MagicDNS hostname, which is not the documented desktop-local pattern. |
| Selected private / partner apps | Plan usage also for “selected private apps” / “selected private clients” [Q] [CB] [DD] | Unverified whether a tailnet household server can join that list without the waitlist. |
| Paid or remotely hosted apps | Complete an interest form / join the waitlist before offering plan usage [OSS] [CB] | Closer to this deployment than a laptop CLI: the app is hosted on the basement PC and used from other devices over Tailscale. |
| Commercial identity sign-in | Website identity (`openid profile email`) is a limited partner trial; registered `oaiapp_…` client and exact HTTPS callback [Q] [WEB] | Identity-only. [WEB] states that ChatGPT plan usage for OSS is a **separate** flow. |

**Client registration.** The OSS plan-usage flow does **not** use a pre-registered partner client. First sign-in sends `client_id=dynamic_agent_client` plus a stable `ext_agent_host_id` and `agent_name_hint`; OpenAI issues a per-user, per-workspace `client_id` (`oaiapp_…`) on the callback. Later sign-ins reuse that issued id. No client secret [REG] [OSS].

**SIWC Terms (binding on a developer who integrates).** Persistent Authentication Tokens “must be local and under the user’s control, **not in a remote or managed environment**” [TERMS §1]. Requests must come from “the user’s local runtime or a remote runtime **only that user controls**”; another user’s activity must not trigger requests on the authenticated user’s account; one user’s subscription must not fulfill another user’s requests [TERMS §2]. “No charge” for using the ChatGPT plan through SIWC [TERMS §2].

**Unverified.** Whether encrypting tokens in this app’s PostgreSQL on the basement PC counts as “local and under the user’s control” versus a “remote or managed environment.” [VM] describes transferring a credential file to a self-hosted VM the same user controls, which is a different pattern from a shared household database. Whether OpenAI would treat MagicDNS hosting as “locally hosted” vs “remotely hosted” is not stated on fetched pages.

## Redirect URI

**OSS plan usage:** HTTP loopback on `127.0.0.1` only, from first registration onward (example `http://127.0.0.1:1455/auth/callback`). Later attempts may change **port only**; scheme, host, and path must stay the same. Do not use `localhost`. The same URI, including port, must be sent on authorize and token exchange [REG]. The cookbook’s Electron example starts a listener on `127.0.0.1` [CB].

**Self-hosted VM:** A `127.0.0.1` callback reaches the machine running the **browser**, not the VM. Documented path: complete OAuth locally, then copy the protected credential file to the VM over a secure channel (for example SSH). Host-specific usage attribution and revocation for transferred sessions are “not yet available” [VM].

**Website identity:** Registered HTTPS callbacks (illustrative `https://example.com/auth/openai/callback`) for **partner** clients [WEB]. No fetched page lists `*.ts.net` as an accepted redirect, for either flow.

**Implication for this app (not a provider fact).** A member signing in from a phone or laptop browser against `https://<host>.<tailnet>.ts.net` cannot complete the documented OSS callback: `127.0.0.1` would hit that client device, not the basement PC. Using a Tailscale HTTPS callback is **undocumented**. The VM transfer procedure is a one-user operator copy, not a household web connect button.

## Tokens

Protocol: OAuth 2.0 authorization code + OIDC + PKCE S256 [Q] [REG]. Plan-usage scopes: `offline_access resource.invoke chatgpt.tokens.use.direct`, with `resource=https://api.openai.com/v1`, plus identity `openid profile email` [REG].

| Item | Documented value |
|---|---|
| Access token lifetime | One hour (`expires_in: 3600`) [TOK] |
| Refresh token | Returned when `offline_access` is granted; 30 days; each successful refresh returns a **replacement** refresh token with a new 30-day lifetime; no fixed count of replacements while each token remains valid [TOK] [SESS] |
| Refresh request | `POST https://auth.openai.com/api/accounts/oauth/token` with `grant_type=refresh_token`, issued `client_id` (not `dynamic_agent_client`), `refresh_token`, and `resource=https://api.openai.com/v1`; omit `scope` to keep the grant; serialize refreshes so two processes do not race a rotating token [SESS] |
| Revocation | `revocation_endpoint` from `https://auth.openai.com/.well-known/openid-configuration`; form POST `token=<REFRESH_TOKEN>`, `token_type_hint=refresh_token`, issued `client_id`; empty HTTP 200, including for an already-invalid token. Revoking the session does not delete the registered client [SESS] |
| Disconnect notice | OpenAI does **not** currently notify the app when the user disconnects in ChatGPT settings; detect on a failed request or refresh, then ask the user to sign in again [ERR] |

Token response fields include `access_token`, `refresh_token`, `id_token`, `token_type`, `expires_in`, `scope`, and `earliest_refresh_at` [TOK]. The meaning of `earliest_refresh_at` beyond being present in the response is **Unverified** (not explained on the fetched token page).

Store tokens in protected local or self-hosted runtime storage; never in browser storage, source control, logs, analytics, or URLs [SESS] [REG]. [TERMS §1] is stricter about “not in a remote or managed environment.”

### Error responses (asked cases)

**Plan usage not enabled.** If the callback has `error=access_denied`, stop and do not exchange a code [REG] [ERR]. After a successful exchange, a valid ID token **without** `chatgpt.tokens.use.direct` is identity-only; mark plan usage disabled and do not call inference [REG] [ERR]. To enable later, repeat OAuth with the saved issued client ID and the full scope set [ERR]. `subscription_sharing_user_not_eligible` (HTTP 403): plan usage unavailable for that user/workspace/policy; do not loop OAuth [ERR].

**Weekly / usage cap reached.** `subscription_sharing_usage_limit_exceeded` (HTTP 429), including as a mid-stream `response.failed` event [ERR] [INF]. Pause ChatGPT-plan requests and link to ChatGPT Settings → Usage. Do not infer a reset time or assume the whole plan is empty; an **app-specific** limit can also apply [ERR] [UX]. Users set a **weekly** per-app cap as a **percentage of overall weekly plan usage** (not a separate allowance) [LEARN]. Plus also has a **five-hour** usage limit **shared across all apps** using the plan; Pro does not have that five-hour limit [SESS]. Credits after the cap are a ChatGPT setting (“Allow other apps to use credits after reaching your usage limit”) [LEARN].

**Re-sign-in required.** Unusable refresh: `invalid_grant`, `invalid_refresh_token`, `token_expired`, `refresh_token_expired`, `refresh_token_invalidated`, or `refresh_token_reused` — clear tokens and repeat OAuth with the saved issued client ID [ERR]. `subscription_sharing_invalid_user` (401): ask the user to sign in again after confirmed revocation or a terminal refresh error [ERR]. Direct-admission 401 before a stream: signed identity or permission not accepted [ERR].

OpenAI does not silently switch billing to another path [ERR].

## Models and features

**Models.** `GET https://api.openai.com/v1/models` with the same access token; keep entries with `visibility: "list"`; show `display_name`, send `slug` as `model` [INF]. Catalog is account-specific; refresh when the ChatGPT account changes [INF]. Fetched examples use `gpt-6.1-sol` [INF]. A fixed public list of eligible slugs is **Unverified**; use the live catalog.

**Endpoint.** `POST https://api.openai.com/v1/responses` with `Authorization: Bearer <access_token>`. Required: `store: false` and `stream: true`. Treat success only after `response.completed` [INF] [LIM]. Do not use ChatGPT `backend-api` endpoints [INF].

**Tool / function calling (needed for #94).** Supported: function/custom tools, grouped in namespaces or supplied as `additional_tools` input items [LIM]. Unsupported on this route: image generation, file search, Code Interpreter, native computer use, hosted MCP/connectors, Responses `tool_search`, and `programmatic_tool_calling` in top-level `tools` [LIM]. Web search remains subject to model and workspace policy [LIM].

**Other preview limits that would affect #91/#94.** Omit `previous_response_id` over HTTP and send history in `input` (no stored Responses conversation) [LIM]. Rejected or unsupported fields include `background`, `conversation`, `max_output_tokens`, `max_tool_calls`, `temperature`, `top_p`, `user`, and others listed on [LIM]. `instructions` or developer messages are used; explicit `{type: "message", role: "system"}` items are rejected [LIM].

**Implication (not a provider fact).** App-provided read-only tools for chat (#94) match “function/custom tools.” They would have to run client-side in this app (as #91 already requires), not as hosted OpenAI tools. Multi-turn chat must resend history because `store` is false.

## Data terms

Fetched, settled:

- Plan usage does **not** grant the app access to the user’s ChatGPT conversations, memories, or API key [Q] [OSS] [LEARN] [CB].
- Inference on this route must set `store: false`, so Responses are not kept as API conversation state for `previous_response_id` [LIM] [INF].
- SIWC Terms: process personal data only as authorized, with a privacy notice before processing; do not collect more than reasonably necessary [TERMS §3].
- Connecting an app does not add a new usage allowance; usage counts toward existing Codex / ChatGPT Work limits [LEARN].

**Unverified (pages timed out or silent):** whether plan-usage request and response bodies are used to train models; retention periods for those payloads; whether they follow consumer ChatGPT data controls or API Platform “API not used for training by default.” [TERMS] does not mention training. Help and “how your data is used” pages were not retrieved on 2026-10-02.

Financial data in prompts or tool results would still leave this machine for every inference call (AGENTS.md / later #106). That is a product risk even if training is off.

## Sign-in method next to Google

OpenAI’s website guide is **identity sign-in** for partner apps, with account create/link on `sub` [WEB] [Q]. The OSS docs still label the button Continue with ChatGPT and show a first-run welcome, but plan usage is a **separate permission** from identity [Q] [UX] [LEARN].

**Proposed in issue #92, recorded as the decision:** do **not** add Sign in with ChatGPT as a household sign-in method next to Google. Keep Google and passwords (#12 / Sign-in methods in docs/requirements.md). ChatGPT would be an **AI connection only**, after the member already has an invited household account. Invitations still apply.

Reasons that match fetched docs and existing product rules: identity SIWC is a partner waitlist with a registered HTTPS client [WEB]; this app already forbids creating members from an external identity without an invitation; Google linking is by `sub` only after invite.

## Recommendation

**Wait.** Do not file a build issue now.

Technically, plan usage can drive Responses with **function/custom tools**, which is what #94 needs [LIM], and billing can sit on a member’s Plus/Pro plan [LEARN]. That is not enough to build:

1. **Redirect.** OSS callbacks are `127.0.0.1` only [REG]. Members use this app in a browser on a tailnet hostname (docs/deployment.md). That OAuth shape does not match. Credential copy per [VM] is not a household connect flow.
2. **Eligibility / terms.** Self-serve OSS is aimed at local (and selected private) tools [OSS] [CB]. A multi-user server plus tokens in the household database sits against [TERMS §1] storage language and [TERMS §2] “runtime only that user controls” / no using one subscription for another user’s requests. #91 already forbids serving other people through one member’s subscription; SIWC terms say the same.
3. **Data training/retention** for plan-usage payloads is Unverified.
4. **#91** already covers the hosting member’s Claude/Codex/Cursor through Agent Harness. SIWC’s unique value is **other members** bringing Plus/Pro without an API key; that value is blocked by (1) and (2).

Revisit if OpenAI documents (a) HTTPS MagicDNS or other non-loopback callbacks for self-hosted web apps, (b) plan usage for a registered confidential client, and (c) token storage on a user-controlled home server that several household members use. Until then, drop is also defensible; wait keeps the door open without a build.

## Routes to access (added 2026-10-02 after the owner chose to pursue access)

1. **Hosting member, self-serve.** [VM] documents the procedure for a self-hosted server:
   - complete OAuth locally with the same client for the same user, through the `127.0.0.1` callback;
   - transfer the protected credential file to the server over a secure channel;
   - persist a stable `ext_agent_host_id` for that server, and let the server own later refreshes.

   On this deployment, the hosting member can open the connect flow in a browser **on the basement PC itself**, so the loopback callback lands on the same machine that runs the app. No transfer step is needed. The app's container must receive that callback, for example through a port published only on host loopback while connecting, or through a small host-side helper. Open points:
   - whether an encrypted token in the app database meets [TERMS §1];
   - that host-specific attribution and revocation are "not yet available" [VM].
2. **Other members: no documented route.** The interest form linked from [OSS] (https://openai.com/form/sign-in-with-chatgpt-interest/) is for commercial integrations. It sends open-source developers to the developer docs, so it is not a path for this app (owner check, 2026-10-02).
   - The self-serve flow needs a `127.0.0.1` callback on the device running the browser.
   - The [VM] transfer assumes a server that only that user controls. Per [TERMS §2] as quoted above, one person's server must not run requests for another person's plan.

   Revisit if OpenAI documents a self-serve pattern for small multi-user self-hosted apps.
3. **Existing alternative.** The hosting member's ChatGPT plan already works through Agent Harness's `codex` backend (#91), with no Sign in with ChatGPT.

## Open follow-ups (only if revisited)

1. Does a basement-PC Docker app count as “local runtime” or “remote/managed” under [TERMS]? Needs OpenAI clarification; do not guess in code.
2. Confirm training/retention on the unread help and data-use pages.
3. If building later: encrypt tokens like SimpleFIN, re-auth to connect/disconnect, per-member only, map `subscription_sharing_*` onto #91 failure codes, `store: false` + stream, function tools only, link Manage usage to ChatGPT Settings → Usage [UX] [LEARN].
