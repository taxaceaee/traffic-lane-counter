# Architecture — TrafficFlow

## Component Map
```
┌─────────────────────────────────────────────────────┐
│                   Frontend (SPA)                    │
│  HTML/JS dashboard, Streamlit dashboard             │
└──────────┬──────────────────────────┬───────────────┘
           │ HTTP REST                 │ WebSocket
           ▼                          ▼
┌──────────────────────────────────────────────────────┐
│                FastAPI Backend                       │
│  ┌────────┐ ┌────────┐ ┌────────┐ ┌──────────────┐ │
│  │ Auth   │ │Camera  │ │ Lanes  │ │ Live/Stream  │ │
│  │ Routes │ │ Routes │ │ Routes │ │ Routes       │ │
│  ├────────┤ ├────────┤ ├────────┤ ├──────────────┤ │
│  │ Jobs   │ │ Counts │ │ Detect │ │ WS Events    │ │
│  │ Routes │ │ Routes │ │ Routes │ │ Routes       │ │
│  └────────┘ └────────┘ └────────┘ └──────────────┘ │
│  ┌────────────────────────────────────────────┐     │
│  │ Middleware: CORS, API Key, Rate Limit,     │     │
│  │            Memory Check                    │     │
│  └────────────────────────────────────────────┘     │
└──────────┬──────────────────────────┬───────────────┘
           │ StorageWorker            │ Redis Pub/Sub
           ▼                          ▼
┌──────────────────┐    ┌────────────────────────┐
│   PostgreSQL     │    │   Redis                │
│ (events, aggs)   │    │ (live event broadcast) │
└──────────────────┘    └────────────────────────┘

               AI Pipeline
┌─────────────────────────────────────────────────┐
│  VideoCapture → YOLOv11 → ByteTrack → Lane      │
│  Assigner → Counting Line → Occupancy Engine    │
│                    ↓                             │
│  StorageWorker (queue, batch commit, DLQ)       │
└─────────────────────────────────────────────────┘
```

## Data Flow
1. **Capture**: Video source → `CameraStreamReader` / `VideoFileReader` / `YouTubeLiveReader`
2. **Detect**: YOLO model (shared via `ModelRegistry` singleton) → detections
3. **Track**: ByteTrack → track IDs
4. **Assign**: Lane polygon containment → lane ID
5. **Count**: Line crossing detection → event
6. **Store**: Crop bytes encoded → StorageWorker queue → batch DB commit
7. **Serve**: REST API queries + WebSocket push + MJPEG stream

## Service Boundaries
| Service | Responsibility | Tech |
|---|---|---|
| AI Pipeline | Frame processing (detect, track, count) | Python, YOLO, ByteTrack |
| Backend API | HTTP endpoints, auth, rate limiting | FastAPI, SQLAlchemy |
| Frontend | Dashboard, live view, lane editor | HTML/JS, Streamlit |
| Storage Worker | Queue management, batch DB writes | Python threading |
| Redis | Live event pub/sub | redis-py |
| PostgreSQL | Event persistence, aggregation | SQLAlchemy, Alembic |
