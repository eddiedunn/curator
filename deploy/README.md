# Curator Deployment Guide

This guide covers deploying the Curator service using Ansible with Quadlet (systemd-managed containers).

## Overview

Curator is an orchestration service that coordinates between Engram (knowledge storage) and Transcribe (audio transcription) services to manage multimedia content ingestion and retrieval.

## Prerequisites

### Infrastructure
- Server with Podman installed and systemd integration (Quadlet)
- Network connectivity to Engram and Transcribe services
- Ansible control node with access to target server

### Required Services
Curator depends on:
- **Engram** (`http://engram:8800`) - Knowledge storage and retrieval
- **Transcribe** (`http://transcribe:8720`) - Audio transcription service

These must be deployed and running before Curator.

## Deployment Steps

### 1. Prepare Directories

Curator requires persistent storage directories on the host:

```bash
# On target server (your-server)
sudo mkdir -p /opt/curator/data
sudo mkdir -p /opt/curator/cache
sudo chown -R $USER:$USER /opt/curator
```

### 2. Configure Environment

Copy the example environment file and adjust values:

```bash
cp ~/code/curator/deploy/curator.env.example ~/code/curator/deploy/curator.env
```

Edit `curator.env` with your configuration:

```bash
# Service URLs (use container names for inter-service communication)
CURATOR_ENGRAM_API_URL=http://engram:8800
CURATOR_TRANSCRIBE_SERVICE_URL=http://transcribe:8720

# Database Configuration
CURATOR_DATABASE_URL=sqlite:////data/curator.db

# Optional: Logging
CURATOR_LOG_LEVEL=INFO
```

**Important**: When services run in the same Podman network (`gpu-services`), use container names (`http://engram:8800`) not `localhost`.

### 3. Deploy Using Ansible

From `your-infra-repo` directory:

```bash
# Deploy Curator service
ansible-playbook -i inventory/hosts.yml playbooks/deploy-gpu-services.yml --tags curator

# Or deploy all GPU services in order
ansible-playbook -i inventory/hosts.yml playbooks/deploy-gpu-services.yml
```

### 4. Manual Deployment (Alternative)

If not using Ansible, deploy manually:

```bash
# 1. Copy source code to target server
rsync -av ~/code/curator/ youruser@your-server:~/code/curator/ \
  --exclude=.git --exclude=__pycache__ --exclude=.venv

# 2. Build container image on target
ssh youruser@your-server 'cd ~/code/curator && podman build -t localhost/curator:latest -f deploy/Dockerfile .'

# 3. Install Quadlet file
scp ~/code/curator/deploy/curator.container youruser@your-server:~/.config/containers/systemd/curator.container

# 4. Reload systemd and start service
ssh youruser@your-server 'systemctl --user daemon-reload && systemctl --user start curator.service'
```

### 5. Verify Deployment

Check service status:

```bash
# On target server
systemctl --user status curator.service

# Check logs
journalctl --user -u curator.service -f
```

Test endpoints:

```bash
# Health check
curl http://localhost:8950/health

# Expected response:
# {"status": "healthy", "service": "curator"}
```

## Container Architecture

### Network Configuration
- **Network**: `gpu-services` (Podman network for inter-service communication)
- **Published Port**: `127.0.0.1:8950:8950` (localhost only)
- **Container Name**: `curator`

### Volume Mounts
- `/opt/curator/data:/data:Z` - SQLite database storage
- `/opt/curator/cache:/cache:Z` - Temporary file cache

### Service Dependencies
Curator starts after:
- `network-online.target`
- `engram.service`
- `transcribe.service`

## API Reference

### Endpoints

```
GET  /health              - Health check
POST /v1/ingest/audio     - Ingest audio file for transcription and storage
GET  /v1/content/{id}     - Retrieve content by ID
POST /v1/search           - Search ingested content
GET  /v1/transcripts/{id} - Get transcript by ID
```

### Example Usage

Ingest audio file:
```bash
curl -X POST http://localhost:8950/v1/ingest/audio \
  -F "file=@recording.mp3" \
  -F "metadata={\"title\":\"Meeting Notes\",\"tags\":[\"meeting\"]}"
```

Search content:
```bash
curl -X POST http://localhost:8950/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query": "project timeline", "limit": 10}'
```

## Troubleshooting

### Service Won't Start

Check Quadlet configuration:
```bash
# View generated unit file
systemctl --user cat curator.service

# Check for errors
systemctl --user daemon-reload
systemctl --user status curator.service
journalctl --user -u curator.service -n 50
```

### Cannot Connect to Engram/Transcribe

Verify network configuration:
```bash
# Check if services are in same network
podman network inspect gpu-services

# Test connectivity from Curator container
podman exec curator curl http://engram:8800/health
podman exec curator curl http://transcribe:8720/health
```

### Database Issues

Check database file permissions:
```bash
ls -la /opt/curator/data/curator.db
```

Reset database (WARNING: deletes all data):
```bash
systemctl --user stop curator.service
rm /opt/curator/data/curator.db
systemctl --user start curator.service
```

### API Errors

Check application logs:
```bash
journalctl --user -u curator.service -f --since "5 minutes ago"
```

Common issues:
- **503 Service Unavailable**: Engram or Transcribe not responding
- **500 Internal Server Error**: Check logs for Python exceptions
- **400 Bad Request**: Invalid request payload

## Updating Curator

### Application Updates

1. Update source code on control machine
2. Re-run Ansible deployment:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/deploy-gpu-services.yml --tags curator
```

Or manually:

```bash
# 1. Sync updated code
rsync -av ~/code/curator/ youruser@your-server:~/code/curator/

# 2. Rebuild image
ssh youruser@your-server 'cd ~/code/curator && podman build -t localhost/curator:latest -f deploy/Dockerfile .'

# 3. Restart service (Quadlet will use new image)
ssh youruser@your-server 'systemctl --user restart curator.service'
```

### Database Migrations

Curator uses SQLite with automatic schema migration on startup (via Alembic or SQLModel).

To check migration status:
```bash
podman exec curator python -m curator.db --check-migrations
```

## Monitoring

### Health Checks
- **Endpoint**: `http://localhost:8950/health`
- **Interval**: 30s (configured in Dockerfile HEALTHCHECK)
- **Timeout**: 10s

### Logs
```bash
# Application logs
journalctl --user -u curator.service -f

# Filter by log level
journalctl --user -u curator.service | grep ERROR
```

### Resource Usage
```bash
podman stats curator
```

## Configuration Reference

See `curator.env.example` for all available environment variables.

Key settings:
- `CURATOR_ENGRAM_API_URL`: Engram service URL (default: `http://engram:8800`)
- `CURATOR_TRANSCRIBE_SERVICE_URL`: Transcribe service URL (default: `http://transcribe:8720`)
- `CURATOR_DATABASE_URL`: SQLite database URL (default: `sqlite:////data/curator.db`)
- `CURATOR_LOG_LEVEL`: Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`)
- `CURATOR_HOST`: Bind address (default: `0.0.0.0`)
- `CURATOR_PORT`: Service port (default: `8950`)

## Integration with Mesh Network

To expose Curator over Tailscale mesh network, configure Caddy reverse proxy:

```
curator.mesh.your-domain.com {
    reverse_proxy localhost:8950
}
```

This allows access from other mesh nodes via:
```bash
curl https://curator.mesh.your-domain.com/health
```

## Backup and Recovery

### Backup Database

```bash
# On target server
cp /opt/curator/data/curator.db /backup/curator-$(date +%Y%m%d).db
```

### Restore Database

```bash
# Stop service
systemctl --user stop curator.service

# Restore database
cp /backup/curator-20260119.db /opt/curator/data/curator.db

# Start service
systemctl --user start curator.service
```

## Security Considerations

1. **Network Isolation**: Curator is only exposed on `127.0.0.1:8950` by default
2. **Inter-Service Auth**: Consider adding API keys for Engram/Transcribe communication
3. **Data Protection**: SQLite database contains sensitive content - ensure proper file permissions
4. **File Uploads**: Validate and sanitize all uploaded files

## Related Documentation

- [Curator API Documentation](../docs/api.md) (if available)
