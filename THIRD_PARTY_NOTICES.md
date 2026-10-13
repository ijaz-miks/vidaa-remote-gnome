# Third-party notices

VIDAA Remote GNOME's application code is licensed under the MIT license in
LICENSE. The project does not redistribute the VIDAA Android app, APKs,
client certificates, private keys, or TV pairing tokens.

## Protocol research

The protocol implementation was informed by native analysis of the VIDAA
Android app with [REA](https://github.com/morluto/rea), and by these MIT-licensed
projects:

- [Warren Rees's pyvidaa](https://github.com/warrenrees/pyvidaa), including its
  earlier myhisense-tv implementation and protocol research.
- [Tom Babolewski's vidaa-control](https://github.com/tombabolewski/vidaa-control).

Their copyright and permission notices are reproduced in licenses/.
This project implements the GNOME interface and local protocol client itself;
it does not vendor those Python packages.

## Runtime dependency

[Eclipse Paho MQTT Python](https://github.com/eclipse-paho/paho.mqtt.python)
2.1.0 is installed as a dependency. It is dual licensed under the Eclipse
Public License 2.0 and Eclipse Distribution License 1.0. Its license remains
with the separately installed package.

GTK, libadwaita, PyGObject, and libsecret are provided by the operating system
under their own licenses. Cryptography is an optional test dependency.

## Names and icon

VIDAA and Hisense names identify compatible products. This is an independent
community project and is not affiliated with or endorsed by those companies.
The included remote-control SVG icon is part of this project's MIT-licensed
artwork.
