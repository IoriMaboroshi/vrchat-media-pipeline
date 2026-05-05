# VRChat Media Pipeline - Remote Server Setup
# 
# Copy to remote server and run as root/sudo

# 1. Create HLS storage directory
mkdir -p /var/www/hls

# 2. Create disk monitoring script
cat > /usr/local/bin/update_hls_disk.sh << 'EOF'
#!/bin/bash
df -h / | tail -1 | awk '{print "{\"total\":\""$2"\",\"used\":\""$3"\",\"avail\":\""$4"\",\"pct\":\""$5"\"}"}' > /var/www/hls/disk.json
EOF
chmod +x /usr/local/bin/update_hls_disk.sh
/usr/local/bin/update_hls_disk.sh

# 3. Set up cron jobs
#    24h auto-cleanup + disk stats every 5 minutes
(crontab -l 2>/dev/null | grep -v 'update_hls_disk\|find.*hls'; echo "0 * * * * find /var/www/hls -type f -mmin +1440 -delete && find /var/www/hls -type d -empty -delete 2>/dev/null") | crontab -
(crontab -l 2>/dev/null | grep -v 'update_hls_disk'; echo "*/5 * * * * /usr/local/bin/update_hls_disk.sh") | crontab -

# 4. Deploy nginx config
cp nginx-remote.conf /etc/nginx/conf.d/vrchat-pipeline.conf
# Edit: replace YOUR_SERVER_IP and your_token_here
nginx -t && systemctl reload nginx

# 5. Upload landing page (from local project)
# Copy deploy/index.html to /var/www/hls/index.html

# 6. Configure rclone on local machine to upload here:
# rclone config create myserver sftp host YOUR_SERVER_IP port 22 user root key_file /path/to/ssh_key
# Then update config.py: ALIWH1_REMOTE = "myserver"
