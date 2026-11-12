"""Send the result of an actual offline data update through configured SMTP."""
from email.message import EmailMessage
import json
import os
import smtplib
import ssl


def main():
    secret = os.environ.get("OFFLINE_UPDATE_SMTP")
    if not secret:
        print("::warning::OFFLINE_UPDATE_SMTP is missing. No update email was sent.")
        return
    config = json.loads(secret)
    success = os.environ["UPDATE_RESULT"] == "success"
    status = "erfolgreich" if success else "fehlgeschlagen"
    message = EmailMessage()
    message["From"] = config["from"]
    message["To"] = config["to"]
    message["Subject"] = f"Feldflug-Offlinedaten: Update {status}"
    run_url = (f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
               f"/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    message.set_content(
        f"Das Offlinedaten-Update war {status}.\n"
        + ("Der geprüfte Katalog ist jetzt aktiv.\n" if success else
           "Der letzte freigegebene Katalog bleibt aktiv.\n")
        + f"GitHub-Lauf: {run_url}\n"
    )
    tls = ssl.create_default_context()
    port = int(config.get("port", 465))
    try:
        if port == 465:
            with smtplib.SMTP_SSL(config["host"], port, timeout=30, context=tls) as smtp:
                smtp.login(config["user"], config["password"])
                smtp.send_message(message)
        else:
            with smtplib.SMTP(config["host"], port, timeout=30) as smtp:
                smtp.starttls(context=tls)
                smtp.login(config["user"], config["password"])
                smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        raise SystemExit(f"Update email failed ({type(error).__name__}).") from None


if __name__ == "__main__":
    main()
