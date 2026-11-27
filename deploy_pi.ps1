# Upload the update package to the Raspberry Pi and run the update script there.
# You will be asked for the Pi password twice (scp, then ssh).
scp D:\zhen\A1ien\AI\plate_detect_update.tar D:\zhen\A1ien\AI\update_pi.sh hc@192.168.100.230:/home/hc/
if ($LASTEXITCODE -ne 0) { Write-Host "Upload failed"; exit 1 }
ssh hc@192.168.100.230 "bash /home/hc/update_pi.sh"
