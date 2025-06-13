# proxy

a reverse proxy that you can plug in front of any OpenAI compatible API
endpoint to handle payments using the Cashu protocol (Bitcoin L3).

Model pricing information is loaded from ``models.json`` by default. If that
file is not present, the bundled ``models.example.json`` will be used. You can
specify a custom path with the ``MODELS_PATH`` environment variable.
