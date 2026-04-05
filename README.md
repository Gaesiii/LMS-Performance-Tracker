# LMS Performance Tracker (Server + Sale Dashboard)

## Run
```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

## Important env vars
- `DATABASE_URL`
- `SALE_DEFAULT_EMAIL` (default: `sale@mindx.local`)
- `SALE_DEFAULT_NAME` (default: `Default Sale`)
- `SALE_DEFAULT_PASSWORD` (for bootstrap manual sale account)
- `SESSION_TTL_HOURS` (default: `24`)
- `GOOGLE_CLIENT_ID` (required for web Google login on `/sale-dashboard`)
- `SALE_ALLOWED_GOOGLE_EMAILS` (comma-separated allow-list)
- `SALE_ALLOWED_GOOGLE_DOMAIN` (optional domain allow-list, e.g. `mindx.edu.vn`)

## Web UI
- Sale dashboard: `GET /sale-dashboard`
- Features:
  - Google login for sale account
  - Manual fallback login
  - Add/update teacher list (UID + email + name)
  - Push trial task to teacher
  - View recent dispatched tasks

## Core APIs

### Sale auth
- `POST /api/auth/sale/login`
- `POST /api/auth/sale/google`
- `POST /api/auth/logout`
- `GET /api/auth/me`

### Teacher directory (for sale)
- `GET /api/sale/teachers`
- `POST /api/sale/teachers/upsert`
- `POST /api/sale/teachers/token`

### Task dispatch (sale -> teacher)
- `POST /api/sale/tasks/push`
- `GET /api/sale/tasks`

### Extension sync (teacher extension)
- `GET /api/trial-tasks?userId=<teacher_uid>&token=<optional_teacher_token>`
- `POST /api/trial-tasks/submit`

## Example: push task
`POST /api/sale/tasks/push`
```json
{
  "tasks": [
    {
      "teacher_uid": "teacher_001",
      "teacher_email": "teacher001@mindx.edu.vn",
      "teacher_name": "Teacher Minh",
      "student_name": "Nguyen Minh Anh",
      "age": 10,
      "course": "Scratch SB",
      "notes": "First trial class",
      "consultant_info": "Sale A",
      "schedule_date": "2026-04-06",
      "schedule_time": "18:30",
      "source": "assigned",
      "data_logs": "pushed from sale dashboard"
    }
  ]
}
```
