# Anthropic workspace end-to-end verification

Tested the current local checkout with BrowserOS against an isolated temporary SQLite database and the real Anthropic API. No upstream credentials are included in this report.

## Results

| Check | Result |
| --- | --- |
| Create Anthropic provider through admin form with optional workspace ID | Passed |
| Reload browser and reopen Edit: workspace ID retained | Passed |
| Admin API returns workspace ID in provider_settings | Passed |
| Non-streaming POST /v1/messages, claude-haiku-4.5, max_tokens=8 | HTTP 200; assistant returned `OK.` |
| Clear workspace ID through Edit and save | Empty value persisted |
| Same Messages request with workspace ID cleared | HTTP 400; Anthropic reported key not scoped to a workspace and requested anthropic-workspace-id |
| Restore workspace ID through Edit and save, without restarting backend | Passed |
| Repeat non-streaming Messages request | HTTP 200; `OK.`; 11 input tokens, 5 output tokens |
| Streaming Messages request | HTTP 200; text/event-stream; `OK.`; message_stop received; no error events |

The successful responses identify the upstream provider as Anthropic. The negative control reproduces the original upstream workspace error, proving that the persisted setting takes effect on real requests and can be cleared/restored through the UI.

## Test conditions

- Local UI: 127.0.0.1:3107; local backend: 127.0.0.1:8107.
- Used a synthetic funded Routstr key in the isolated database; no real Cashu balance was modified.
- Real Anthropic requests used the operator-supplied upstream credentials, entered through the provider form.
- No workspace header was supplied by the request client: the provider configuration added it upstream.
- Browser-origin requests needed `anthropic-dangerous-direct-browser-access: true` because the proxy forwards Origin upstream.
- The mapped `claude-haiku-4.5` model succeeded. A preliminary `claude-haiku-5.5` request returned HTTP 404 suggesting `claude-haiku-5-5`; that separate model-name mapping issue is outside this workspace-setting change.
- Temporary servers, browser test tab, and credential-bearing temporary database are removed after verification.
