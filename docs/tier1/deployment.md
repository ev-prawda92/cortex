# Deployment Guide

This guide covers deploying Cortex to dev, staging, and production environments. By the end, you'll have a live Cortex instance that can operate agents at scale.

## Prerequisites

- Docker and Docker Compose (for any deployment)
- PostgreSQL 13+ (for production) or SQLite (for dev)
- API keys for providers (Anthropic, OpenAI, etc.)

## Local Development (Docker Compose)

**Best for:** Testing, local development, demos. Not for production data.

### 1. Clone and configure

```bash
git clone https://github.com/your-org/cortex.git
cd cortex

# Copy example env file
cp .env.example .env

# Edit .env with your API keys
# ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
```

### 2. Start services

```bash
docker compose up --build
```

Services started:
- **Cortex API:** http://localhost:8000
- **PostgreSQL:** localhost:5432 (default in docker-compose)
- **Dashboard:** http://localhost:8000 (same as API)

### 3. Verify it's working

```bash
curl http://localhost:8000/api/health
# Should return: {"status": "ok"}
```

### 4. Stop when done

```bash
docker compose down
```

To wipe all data (fresh slate):
```bash
docker compose down -v
```

## Staging Environment (Cloud Deployment)

**Best for:** Testing before production, load testing, team collaboration.

### Option A: Deploy to AWS (EC2 + RDS)

**1. Create RDS PostgreSQL instance:**
```bash
# Use AWS console or CLI
aws rds create-db-instance \
  --db-instance-identifier cortex-staging \
  --db-instance-class db.t3.micro \
  --engine postgres \
  --master-username cortex_user \
  --master-user-password <strong-password> \
  --allocated-storage 20
```

Get the endpoint after creation (e.g., `cortex-staging.xxxxx.us-east-1.rds.amazonaws.com`).

**2. Create EC2 instance (or ECS):**
```bash
# Create t3.small instance
# Security group: allow inbound on 8000 (Cortex API)
# SSH in and clone Cortex
git clone https://github.com/your-org/cortex.git
cd cortex
```

**3. Configure environment:**
```bash
cat > .env << EOF
DATABASE_URL=postgresql://cortex_user:<password>@cortex-staging.xxxxx.us-east-1.rds.amazonaws.com:5432/cortex
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
ENVIRONMENT=staging
EOF
```

**4. Start Cortex:**
```bash
docker compose up -d
```

**5. Run database migrations:**
```bash
docker exec cortex-api python migrate.py
```

**6. Access the dashboard:**
```
http://<ec2-instance-ip>:8000
```

### Option B: Deploy to Heroku

```bash
# Install Heroku CLI
brew install heroku

# Create app
heroku create cortex-staging

# Add PostgreSQL add-on
heroku addons:create heroku-postgresql:standard-0 --app cortex-staging

# Set environment variables
heroku config:set ANTHROPIC_API_KEY=sk-ant-... --app cortex-staging
heroku config:set OPENAI_API_KEY=sk-... --app cortex-staging

# Deploy
git push heroku main

# Run migrations
heroku run python migrate.py --app cortex-staging
```

Your dashboard will be at `https://cortex-staging.herokuapp.com`.

### Option C: Deploy to Kubernetes (self-hosted or managed)

```bash
# Create namespace
kubectl create namespace cortex

# Create secrets
kubectl create secret generic cortex-env \
  --from-literal=ANTHROPIC_API_KEY=sk-ant-... \
  --from-literal=OPENAI_API_KEY=sk-... \
  -n cortex

# Create ConfigMap for DATABASE_URL
kubectl create configmap cortex-config \
  --from-literal=DATABASE_URL=postgresql://user:pass@postgres:5432/cortex \
  -n cortex

# Apply Cortex deployment
cat > cortex-deployment.yaml << EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cortex-api
  namespace: cortex
spec:
  replicas: 2
  selector:
    matchLabels:
      app: cortex-api
  template:
    metadata:
      labels:
        app: cortex-api
    spec:
      containers:
      - name: cortex-api
        image: your-registry/cortex:latest
        envFrom:
        - secretRef:
            name: cortex-env
        - configMapRef:
            name: cortex-config
        ports:
        - containerPort: 8000
        resources:
          requests:
            memory: "256Mi"
            cpu: "250m"
          limits:
            memory: "512Mi"
            cpu: "500m"
---
apiVersion: v1
kind: Service
metadata:
  name: cortex-api
  namespace: cortex
spec:
  selector:
    app: cortex-api
  type: LoadBalancer
  ports:
  - port: 80
    targetPort: 8000
EOF

kubectl apply -f cortex-deployment.yaml
```

Get the service IP:
```bash
kubectl get svc cortex-api -n cortex
```

## Production Environment

**Best for:** Running Cortex for real agents, real clients, real uptime.

### Required Setup

1. **Database:** PostgreSQL 13+ (managed service strongly recommended)
   - RDS, Cloud SQL, or self-managed
   - Multi-AZ for redundancy
   - Automated backups
   - Security group: only allow Cortex servers

2. **Cortex Servers:** At least 2 instances (load balanced)
   - t3.medium or larger (2 CPU, 4GB RAM minimum)
   - Auto-scaling group (scale 2-10 based on load)
   - Health checks every 30 seconds

3. **Load Balancer:** ALB (AWS) or equivalent
   - HTTPS only (certificate from ACM or LetsEncrypt)
   - Route `/api/*` to Cortex servers
   - Target group health check: `/api/health`

4. **Logging & Monitoring:** (see [Monitoring](../tier2/monitoring.md))
   - CloudWatch, Datadog, or ELK for logs
   - Alarms for high error rates, CPU, database connection pool

### Deployment Script (AWS)

```bash
#!/bin/bash

# Create secrets in AWS Secrets Manager
aws secretsmanager create-secret \
  --name cortex/production \
  --secret-string '{"ANTHROPIC_API_KEY":"sk-ant-...","OPENAI_API_KEY":"sk-..."}'

# Create RDS PostgreSQL
aws rds create-db-instance \
  --db-instance-identifier cortex-prod \
  --db-instance-class db.t3.large \
  --engine postgres \
  --allocated-storage 100 \
  --multi-az

# Create ALB
aws elbv2 create-load-balancer \
  --name cortex-alb \
  --subnets subnet-xxxxx subnet-yyyyy

# Create target group
aws elbv2 create-target-group \
  --name cortex-tg \
  --protocol HTTP \
  --port 8000 \
  --health-check-path /api/health

# Create launch template
aws ec2 create-launch-template \
  --launch-template-name cortex-template \
  --launch-template-data '{
    "ImageId":"ami-...",
    "InstanceType":"t3.medium",
    "IamInstanceProfile":{"Name":"cortex-instance-role"},
    "UserData":"...base64-encoded startup script..."
  }'

# Create auto-scaling group
aws autoscaling create-auto-scaling-group \
  --auto-scaling-group-name cortex-asg \
  --launch-template LaunchTemplateName=cortex-template \
  --min-size 2 \
  --max-size 10 \
  --desired-capacity 2 \
  --load-balancer-names cortex-alb
```

### Configuration for Production

Create `.env`:
```bash
# Database
DATABASE_URL=postgresql://cortex_user:strong_password@cortex-prod.xxxxx.us-east-1.rds.amazonaws.com:5432/cortex

# Providers
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

# Environment
ENVIRONMENT=production
DEBUG=false

# Security
SECRET_KEY=<generate with: python -c "import secrets; print(secrets.token_urlsafe(32))">
ALLOWED_ORIGINS=https://cortex.company.com

# Logging
LOG_LEVEL=info
LOG_FORMAT=json  # for structured logging to CloudWatch/ELK

# Performance
WORKER_PROCESSES=4
WORKER_THREADS=8
CONNECTION_POOL_SIZE=20
```

### Run Migrations

```bash
# SSH into one server, run once
DATABASE_URL=postgresql://... python migrate.py

# Verify all tables created
DATABASE_URL=postgresql://... python -c "
from db import engine, Base
from sqlalchemy import inspect
inspector = inspect(engine)
print('Tables:', inspector.get_table_names())
"
```

### Health Checks

```bash
# Verify database connection
curl https://cortex.company.com/api/health

# Should return: {"status": "ok", "database": "connected"}
```

### Backup and Recovery

```bash
# Automated RDS backups (AWS): enabled by default, 30-day retention
# Manual backup before major changes:
aws rds create-db-snapshot \
  --db-instance-identifier cortex-prod \
  --db-snapshot-identifier cortex-prod-backup-$(date +%Y%m%d)

# Restore from backup if needed:
aws rds restore-db-instance-from-db-snapshot \
  --db-instance-identifier cortex-prod-restored \
  --db-snapshot-identifier cortex-prod-backup-20260827
```

### Updates and Rollout

```bash
# Build and push new image
docker build -t your-registry/cortex:v1.2.3 .
docker push your-registry/cortex:v1.2.3

# Update launch template with new image
aws ec2 create-launch-template-version \
  --launch-template-name cortex-template \
  --source-version 1 \
  --launch-template-data '{
    "ImageId":"ami-...",
    "InstanceType":"t3.medium"
  }'

# Rolling update via auto-scaling group
aws autoscaling update-auto-scaling-group \
  --auto-scaling-group-name cortex-asg \
  --launch-template LaunchTemplateName=cortex-template,Version=2

# Terminate old instances (one at a time)
aws ec2 terminate-instances --instance-ids i-xxxxx
# ASG will spin up a new one with new image
```

## Monitoring Your Deployment

See [Monitoring & Alerting](../tier2/monitoring.md) for setting up observability.

Quick checks:
- **API Health:** `curl https://cortex.company.com/api/health`
- **Database:** Check RDS CloudWatch metrics (CPU, connections, disk)
- **Errors:** Set up log aggregation (CloudWatch, ELK, Datadog)
- **Latency:** Track API response times
- **Agents:** Use Cortex's own Monitor tab to track agent health

## Troubleshooting

**"Unable to connect to database"**
- Verify DATABASE_URL is correct
- Check security group allows Cortex servers to reach RDS
- Check RDS is in the same VPC

**"Agents not executing"**
- Verify API keys are set (ANTHROPIC_API_KEY, etc.)
- Check daemon is running: `curl https://cortex.company.com/api/daemon/status`
- Check logs for errors: `docker logs cortex-api` or CloudWatch

**"High latency on API calls"**
- Scale up instance size (t3.medium → t3.large)
- Increase connection pool size in .env
- Check if database is bottleneck (RDS CPU/connections)

**"Out of disk space in RDS"**
- RDS will auto-scale if storage autoscaling is enabled (recommended)
- Or manually resize: `aws rds modify-db-instance --db-instance-identifier cortex-prod --allocated-storage 200`

## Next Steps

- **Monitor your deployment:** [Monitoring & Alerting](../tier2/monitoring.md)
- **Set up team access:** [Multi-Tenant Setup](../tier2/multi-tenant.md)
- **Deploy your first agent:** [Agent Integration Guide](./agent-integration.md)
