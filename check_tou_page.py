import requests, re

r = requests.get('https://www.sdge.com/total-electric-rates', timeout=30)
html = r.text

# Print the raw HTML of the EV-TOU2 accordion panel
idx = html.find('EV-TOU2', 1120000)
# Find the panel-body that follows
start = html.find('panel-body', idx)
end = html.find('</div>', start + 500)
# Get a bigger chunk
chunk = html[idx:idx+3000]
print(chunk)
