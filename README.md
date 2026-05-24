# QR Generator

Flet web app for creating headshot QR sessions.

## Run With Docker Compose

1. Optional: copy `.env.example` to `.env` and fill in SMTP settings if you want email delivery.
2. Build and start the service:

```powershell
docker compose up -d --build
```

3. Open the app at `http://localhost:8080`.

SQLite data is stored in the named Docker volume `qr-generator-data` at `/data/headshots.db` inside the container.

Useful commands:

```powershell
docker compose logs -f
docker compose down
docker compose down -v
```

`docker compose down -v` removes the database volume, so use it only when you want to delete persisted app data.
