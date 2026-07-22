#!/bin/bash
# HRMS Oracle Cloud Setup Script
# Run on a fresh Ubuntu 22.04/24.04 VM
# Usage: bash setup.sh

set -e

REPO_URL="https://github.com/ShubhamvijayJawalkar/HRMS-Portal.git"
APP_DIR="/opt/hrms"
DB_DIR="/opt/hrms/data"
APP_USER="hrms"

echo "=== HRMS Deployment - Oracle Cloud Free Tier ==="

# 1. System updates
echo "[1/8] Updating system..."
sudo apt update && sudo apt upgrade -y

# 2. Install dependencies
echo "[2/8] Installing Python and system packages..."
sudo apt install -y python3 python3-pip python3-venv nginx git

# 3. Create app user
echo "[3/8] Creating app user..."
if ! id "$APP_USER" &>/dev/null; then
    sudo useradd -r -s /bin/false $APP_USER
fi

# 4. Clone app
echo "[4/8] Cloning HRMS repository..."
if [ -d "$APP_DIR" ]; then
    cd $APP_DIR && sudo git pull
else
    sudo git clone $REPO_URL $APP_DIR
fi

# 5. Setup Python venv
echo "[5/8] Setting up Python environment..."
cd $APP_DIR
sudo python3 -m venv venv
sudo chown -R $APP_USER:$APP_USER $APP_DIR
sudo -u $APP_USER $APP_DIR/venv/bin/pip install --upgrade pip
sudo -u $APP_USER $APP_DIR/venv/bin/pip install -r requirements.txt

# 6. Create data directory for DuckDB
echo "[6/8] Creating persistent data directory..."
sudo mkdir -p $DB_DIR
sudo chown -R $APP_USER:$APP_USER $DB_DIR

# 7. Create systemd service
echo "[7/8] Creating systemd service..."
sudo tee /etc/systemd/system/hrms.service > /dev/null <<EOF
[Unit]
Description=HRMS Flask Application
After=network.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/gunicorn --workers 1 --bind 127.0.0.1:5000 app:app
Restart=always
RestartSec=5
Environment="DB_FILE=$DB_DIR/hrms.duckdb"
Environment="FLASK_ENV=production"
Environment="SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

[Install]
WantedBy=multi-user.target
EOF

# 8. Configure Nginx
echo "[8/8] Configuring Nginx..."
sudo tee /etc/nginx/sites-available/hrms > /dev/null <<'EOF'
server {
    listen 80;
    server_name _;

    client_max_body_size 10M;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 60s;
    }

    location /static/ {
        alias $APP_DIR/static/;
        expires 1d;
    }
}
EOF

# Enable site
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf /etc/nginx/sites-available/hrms /etc/nginx/sites-enabled/
sudo nginx -t

# 9. Open firewall
echo "[9/9] Configuring firewall..."
sudo apt install -y ufw
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable

# 10. Start services
echo "Starting HRMS..."
sudo systemctl daemon-reload
sudo systemctl enable hrms
sudo systemctl start hrms
sudo systemctl enable nginx
sudo systemctl restart nginx

echo ""
echo "=== DEPLOYMENT COMPLETE ==="
echo "HRMS is running at: http://$(curl -s ifconfig.me)"
echo ""
echo "Commands:"
echo "  sudo systemctl status hrms    # Check app status"
echo "  sudo systemctl restart hrms   # Restart app"
echo "  sudo journalctl -u hrms -f    # View logs"
echo "  cd /opt/hrms && sudo git pull && sudo systemctl restart hrms  # Update"
