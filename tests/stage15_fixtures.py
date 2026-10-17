"""Stage 15 test fixtures: the real ALD receipt e-mail, sanitized.

Source: the employer's receipt confirmation fetched read-only from the
application mailbox on 2026-10-17 (HTML-only message, text reduced with
app.utils.html_text). Kept verbatim except: the applicant's name, every
address, phone/fax numbers, the employer's street and the Message-ID are
replaced. Nothing here is a real credential or contact detail.
"""

from datetime import UTC, datetime

ALD_RECEIPT_SUBJECT = "Ihre Bewerbung - Eingangsbestätigung"
ALD_RECEIPT_FROM = "karriere@ald-vt.de"
ALD_RECEIPT_DISPLAY_NAME = "ALD Vacuum Technologies GmbH"
ALD_RECEIPT_DATE_HEADER = datetime(2026, 10, 17, 14, 2, 50, tzinfo=UTC)  # 16:02:50+02:00
ALD_RECEIPT_ARRIVAL = datetime(2026, 10, 17, 14, 6, 17, tzinfo=UTC)
ALD_RECEIPT_MESSAGE_ID = "<stage15-fixture-ald-receipt@ald-vt.de>"
ALD_SUBMITTED_AT = datetime(2026, 10, 17, 14, 2, 50, tzinfo=UTC)

ALD_RECEIPT_TEXT = """Sehr geehrter Herr Mustermann,
vielen Dank für Ihr Interesse an einer Mitarbeit bei ALD Vacuum Technologies GmbH und die \
Zusendung Ihrer Bewerbung, deren Eingang wir hiermit bestätigen.
Sobald wir Ihre Bewerbungsunterlagen eingehend geprüft haben, werden wir uns zeitnah mit \
weiterführenden Informationen zum kommenden Prozess bei Ihnen melden. Wir bitten um Ihr \
Verständnis, dass dies noch etwas Zeit in Anspruch nehmen kann.
Bei Rückfragen stehen wir Ihnen selbstverständlich gerne zur Verfügung.
Mit freundlichen Grüßen / Yours Sincerely
ALD Recruiting Team
ALD Vacuum Technologies GmbH
<street redacted>
63457 Hanau
Germany
Tel: <redacted> | Fax: <redacted> | http://www.ald-vt.de
powered by www.rexx-systems.com"""

# The same content as the HTML-only MIME message the employer actually sent.
ALD_RECEIPT_HTML = (
    "<html><head><style>p{margin:0}</style></head><body>"
    + "".join(f"<p>{line}</p>" for line in ALD_RECEIPT_TEXT.splitlines())
    + "</body></html>"
)


def ald_receipt_mime() -> bytes:
    from email.mime.text import MIMEText

    message = MIMEText(ALD_RECEIPT_HTML, "html", "utf-8")
    message["Subject"] = ALD_RECEIPT_SUBJECT
    message["From"] = f'"{ALD_RECEIPT_DISPLAY_NAME}" <{ALD_RECEIPT_FROM}>'
    message["To"] = "applicant@example.invalid"
    message["Date"] = "Sat, 17 Oct 2026 16:02:50 +0200"
    message["Message-ID"] = ALD_RECEIPT_MESSAGE_ID
    return message.as_bytes()
