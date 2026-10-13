# Security reports

Use GitHub's private vulnerability reporting feature on this repository for
security issues. Do not put credentials, certificates, tokens, PINs, or private
network information in a public issue.

The current development version is supported. Reviewers should pay particular
attention to desktop identity isolation, certificate pinning before MQTT login,
token expiry and refresh, bounded parsing, and the phone-mode focus guard.

The application does not bundle the vendor's client credentials and does not
copy authorization from another paired client. Supply certificates locally and
pair the desktop with the TV's on-screen code.
