#!/bin/bash
# Setup script for NAS mount with overlay rootfs
# Run this ON THE PI as user (not root)

set -e

echo "Setting up NAS mount for overlay rootfs..."

# Ensure mount point exists (will be in upper layer)
sudo mkdir -p /mnt/nas

# Copy systemd units
sudo cp mnt-nas.mount /etc/systemd/system/
sudo cp mnt-nas.automount /etc/systemd/system/

# Reload and enable
sudo systemctl daemon-reload
sudo systemctl enable mnt-nas.automount

echo "Done! Mount will be available on first access."
echo "Test with: ls /mnt/nas"
echo "After reboot, check with: systemctl status mnt-nas.automount"
