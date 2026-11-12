# Python examples

Choose a node from the [provider directory](https://routstr.com/providers), then
set `API_URL` to its OpenAI-compatible base URL (the node's URL plus `/v1`).
`https://provider.example` below is a placeholder: replace it with your chosen
node's URL.

```bash
export API_URL="https://provider.example/v1"
export TOKEN="your-funded-api-key-or-cashu-token"
python examples/list_models.py
python examples/chat_completions.py
```

Check the node's `/v1/models` endpoint before sending payment and choose a model
it supports. Some examples also accept `MODEL`; others have a model in the script
that you should replace if necessary.

`API_URL` is required: these scripts do not default to a hosted provider.
`api.routstr.com` is currently unavailable. Never send real tokens to the
placeholder URL.
