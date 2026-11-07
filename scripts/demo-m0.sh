#!/bin/sh
# M0 demonstration: ask a running Umbrous runtime to do things it must refuse.
# Start the runtime first (bin/umbrous), then run: scripts/demo-m0.sh [port]
PORT="${1:-8417}"
API="http://127.0.0.1:$PORT/api/health"

show() { printf '\n== %s\n$ %s\n' "$1" "$2"; sh -c "$2"; echo; }

show "1. No session credential (expect 401)" \
  "curl --noproxy '*' -s -w ' -> HTTP %{http_code}' $API"
show "2. A made-up credential (expect 401)" \
  "curl --noproxy '*' -s -w ' -> HTTP %{http_code}' -H 'X-Umbrous-Token: guess' $API"
show "3. A request claiming to come from another web site (expect 403)" \
  "curl --noproxy '*' -s -w ' -> HTTP %{http_code}' -H 'Origin: https://evil.example' -H 'X-Umbrous-Token: guess' $API"
show "4. DNS rebinding: right address, wrong Host name (expect 403)" \
  "curl --noproxy '*' -s -w ' -> HTTP %{http_code}' -H 'Host: evil.example:$PORT' $API"
show "5. CORS preflight from another site (expect 403, no Access-Control headers)" \
  "curl --noproxy '*' -s -i -X OPTIONS -H 'Origin: https://evil.example' -H 'Access-Control-Request-Method: GET' $API | grep -iE '^HTTP|^access-control' "

# 6. This machine's network address, as another computer on the Wi-Fi would use it.
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')"
if [ -n "$LAN_IP" ]; then
  show "6. Same port on this machine's network address $LAN_IP (expect connection refused, code 7)" \
    "curl --noproxy '*' -s -m 3 http://$LAN_IP:$PORT/api/health; echo \"curl exit code \$?\""
else
  echo "6. Skipped: no network address found."
fi
cat <<'MSG'

== 7. The allowed path
The page itself holds the credential. In the Umbrous window open the developer
console (Cmd-Option-I) and run:
    sessionStorage.getItem('umbrous.session')
then, in a terminal:
    curl --noproxy '*' -s -H "X-Umbrous-Token: <that value>" http://127.0.0.1:8417/api/health
That works, which is the point of "a layer, not a guarantee": anything that can
read the page (an extension, malware running as you) can use the credential.
MSG
