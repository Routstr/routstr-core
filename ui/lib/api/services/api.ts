import axios from 'axios';
import type { z } from 'zod';

const PRODUCTION = 'production';

export enum HTTPMethod {
  GET = 'GET',
  POST = 'POST',
}

export enum HTTPStatusCode {
  OK = 200,
}

// Define the Nostr Event interface
interface NostrEvent {
  kind: number;
  created_at: number;
  content: string;
  tags: string[][];
}

interface NostrProvider {
  signEvent: (event: NostrEvent) => Promise<NostrEvent>;
}

type WindowWithNostr = Window & {
  nostr?: NostrProvider;
};

function getNostrProvider(): NostrProvider {
  if (typeof window === 'undefined') {
    throw new Error('Nostr provider is unavailable on the server');
  }

  const windowWithNostr = window as WindowWithNostr;
  if (
    !windowWithNostr.nostr ||
    typeof windowWithNostr.nostr.signEvent !== 'function'
  ) {
    throw new Error('Nostr provider is unavailable');
  }

  return windowWithNostr.nostr;
}

export default function api<Request, Response>({
  method,
  path,
  requestSchema,
  responseSchema,
}: {
  method: HTTPMethod;
  path: string;
  requestSchema: z.ZodType<Request>;
  responseSchema: z.ZodType<Response>;
}): (data: Request) => Promise<Response> {
  return function (requestData: Request) {
    requestSchema.parse(requestData);

    async function apiCall() {
      const nostr = getNostrProvider();
      const auth_event = await nostr.signEvent({
        kind: 27235,
        created_at: Math.floor(new Date().getTime() / 1000),
        content: 'application/json',
        tags: [
          ['u', `${process.env.API_URL}${path}`],
          ['method', method],
        ],
      });

      const response = await axios({
        baseURL: process.env.NEXT_PUBLIC_API_URL,
        headers: {
          authorization: `Nostr ${btoa(JSON.stringify(auth_event))}`,
          'Content-Type': 'application/json',
        },
        method,
        url: path,
        [method === HTTPMethod.GET ? 'params' : 'data']: requestData,
      });

      if (process.env.NODE_ENV === PRODUCTION) {
        return response.data as Response;
      }

      return responseSchema.parse(response.data);
    }

    return apiCall();
  };
}
