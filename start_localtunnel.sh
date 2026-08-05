#!/bin/bash
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"  # This loads nvm

while true; do
  npx -y localtunnel --port 8000 --subdomain fancy-rooms-bet
  sleep 2
done
