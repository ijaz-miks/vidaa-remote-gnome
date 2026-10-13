# Contributing

Bug reports and pull requests are welcome. Include the Ubuntu version, GTK and
libadwaita versions, TV model, firmware version if comfortable sharing it, and
the discovery transport protocol. Describe the expected and actual behavior.

Do not include pairing tokens, PINs, client certificates, private keys, APKs,
phone identifiers, MAC addresses, personal network settings, or raw packet
captures. Remove device and account data from screenshots and logs. The demo
preview is suitable for interface screenshots without exposing real settings.

Use the development environment and tests in the README. Protocol changes should
include a focused test for the changed behavior. Keep the GTK interface responsive
by running network and ADB operations outside the UI thread. Preserve token
isolation, TLS pinning, and the guard against tapping another phone application.

Compatibility claims should distinguish source-based assumptions from behavior
verified on real hardware. Opening a connection does not prove that a TV command
was applied.
