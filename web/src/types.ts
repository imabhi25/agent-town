// Shapes sent by the backend (see app/hub.py and app/event_publisher.py).

export interface TownEvent {
  id: number; // Postgres id: the unique key used to drop duplicates
  stream_id: string;
  type: string;
  payload: Record<string, unknown>;
  ts: string; // created_at, ISO 8601
}

export interface Snapshot {
  type: "snapshot";
  goals: Record<string, unknown>[];
  tasks: Record<string, unknown>[];
  workers: { id: string; job_id: string | null }[];
  queue: Record<string, number>;
  stats: Record<string, unknown>;
  dead_jobs?: { job_id: string; type: string }[];
  events: TownEvent[];
  last_event_id: string;
}

export interface EventBatch {
  type: "events";
  events: TownEvent[];
  dropped?: number; // the server dropped this many events for us: we have a gap
}

export type ServerMessage = Snapshot | EventBatch;
