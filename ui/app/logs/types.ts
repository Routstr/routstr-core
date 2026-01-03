export interface LogEntry {
  asctime: string;
  name: string;
  levelname: string;
  message: string;
  pathname: string;
  lineno: number;
  version: string;
  request_id: string;
  [key: string]: string | number | object | undefined;
}

export interface LogsResponse {
  logs: LogEntry[];
  total: number;
  date: string | null;
  level: string | null;
  request_id: string | null;
  search: string | null;
  status_codes: string | null;
  methods: string | null;
  endpoints: string | null;
  limit: number;
}

export interface DatesResponse {
  dates: string[];
}
