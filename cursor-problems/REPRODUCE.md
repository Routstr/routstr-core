# Reproducing Cursor Problems

Each subdirectory contains a `request.json` (the request body) and `response.json` (the error response received).

## Using curl to reproduce

From the `routstr-core/` directory:

```bash
# OpenAI model error
curl -X POST https://staging.routstr.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d @cursor-problems/openai-model-error/request.json

# Anthropic internal error
curl -X POST https://staging.routstr.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d @cursor-problems/anthropic-internal-error/request.json

# Model not found error
curl -X POST https://staging.routstr.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d @cursor-problems/model-not-found-error/request.json

# Upstream rate limit error
curl -X POST https://staging.routstr.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d @cursor-problems/upstream-rate-limit-error/request.json
```

## Generic pattern

```bash
curl -X POST <API_ENDPOINT> \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d @cursor-problems/<directory>/request.json
```

The `-d @filename` syntax tells curl to read the request body from a file.
