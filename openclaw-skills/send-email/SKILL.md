---
name: send-email
description: Send emails via Outlook/Hotmail SMTP
requires:
  env:
    - SMTP_EMAIL
    - SMTP_PASSWORD
  bins:
    - python3
---

# Send Email

Send an email using the configured Outlook/Hotmail SMTP account.

## Usage

```bash
python3 -c "
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os

smtp_email = os.environ['SMTP_EMAIL']
smtp_password = os.environ['SMTP_PASSWORD']

msg = MIMEMultipart()
msg['From'] = smtp_email
msg['To'] = '[RECIPIENT_EMAIL]'
msg['Subject'] = '[SUBJECT]'

body = '''[EMAIL_BODY]'''
msg.attach(MIMEText(body, 'plain'))

with smtplib.SMTP('smtp-mail.outlook.com', 587) as server:
    server.starttls()
    server.login(smtp_email, smtp_password)
    server.send_message(msg)
    print('Email sent successfully')
"
```

## Send HTML Email

```bash
python3 -c "
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os

smtp_email = os.environ['SMTP_EMAIL']
smtp_password = os.environ['SMTP_PASSWORD']

msg = MIMEMultipart('alternative')
msg['From'] = smtp_email
msg['To'] = '[RECIPIENT_EMAIL]'
msg['Subject'] = '[SUBJECT]'

html = '''[HTML_CONTENT]'''
msg.attach(MIMEText(html, 'html'))

with smtplib.SMTP('smtp-mail.outlook.com', 587) as server:
    server.starttls()
    server.login(smtp_email, smtp_password)
    server.send_message(msg)
    print('Email sent successfully')
"
```

## Send with Attachment

```bash
python3 << 'PYEOF'
import smtplib, os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

smtp_email = os.environ['SMTP_EMAIL']
smtp_password = os.environ['SMTP_PASSWORD']

msg = MIMEMultipart()
msg['From'] = smtp_email
msg['To'] = '[RECIPIENT_EMAIL]'
msg['Subject'] = '[SUBJECT]'
msg.attach(MIMEText('[EMAIL_BODY]', 'plain'))

# Attach file
filepath = '[FILE_PATH]'
with open(filepath, 'rb') as f:
    part = MIMEBase('application', 'octet-stream')
    part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header('Content-Disposition', f'attachment; filename={os.path.basename(filepath)}')
    msg.attach(part)

with smtplib.SMTP('smtp-mail.outlook.com', 587) as server:
    server.starttls()
    server.login(smtp_email, smtp_password)
    server.send_message(msg)
    print('Email sent successfully with attachment')
PYEOF
```

## Rules

- Always confirm with the user before sending an email
- Show the recipient, subject, and body preview before sending
- Use plain text by default, HTML only when formatting is needed
- Keep subjects concise and descriptive
- For trade reports, include key data points in the subject line
