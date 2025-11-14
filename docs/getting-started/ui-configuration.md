# UI Configuration

This guide explains how to configure the Routstr UI for different environments.

## Environment Variables

The UI uses Next.js environment variables to configure API endpoints and authentication.

### Centralized Configuration

This project uses a centralized configuration approach with a single `.env` file in the project root. This file contains both backend and frontend configuration variables.

Create or update your `.env` file in the project root:

```bash
# .env (in project root)

# UI Configuration (NEXT_PUBLIC_ variables are exposed to the browser)
NEXT_PUBLIC_API_URL=http://127.0.0.1:8000
```

### Development vs Production

The same `.env` file is used for both development and production. Simply change the values:

**Development:**

```bash
NEXT_PUBLIC_API_URL=http://127.0.0.1:8000
```

**Production:**

```bash
NEXT_PUBLIC_API_URL=https://api.yourroutstr.com
```

## Building the UI

The build process automatically reads configuration from the root `.env` file:

```bash
# From the project root
make ui-build
# or
./scripts/build-ui.sh
```

The build script will automatically:

- Load `NEXT_PUBLIC_*` variables from the root `.env` file
- Use them during the Next.js build process
- Display warnings if the `.env` file is missing
