# Upload bind_pi.sh and run it on the Raspberry Pi (password asked twice).
scp D:\zhen\A1ien\AI\bind_pi.sh hc@192.168.100.230:/home/hc/
if ($LASTEXITCODE -ne 0) { Write-Host "Upload failed"; exit 1 }
ssh hc@192.168.100.230 "bash /home/hc/bind_pi.sh"
