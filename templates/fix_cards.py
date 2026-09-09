import os
import re

d = "/Users/deanjuandcunha/Desktop/Freelance Projects/smart_hub/templates"
for root, _, files in os.walk(d):
    for f in files:
        if f.endswith(".html") and f != "admin.html" and f != "base.html":
            path = os.path.join(root, f)
            with open(path, "r") as file:
                content = file.read()
            
            # replace class="card"
            content = content.replace('class="card"', 'class="admin-card"')
            # replace class="card ...
            content = re.sub(r'class="card\s+', 'class="admin-card ', content)
            
            # optionally clean up shadow and borders from admin-card
            # but leave it simple for now, as admin-card handles most styling
            
            # replace card-header bg-XXX text-white because admin-card has its own header style 
            # actually replacing bg-light is good
            content = content.replace('card-header bg-light', 'card-header')
            
            with open(path, "w") as file:
                file.write(content)
