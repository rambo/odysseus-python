Creating image for running:

* Base image `2019-04-08-raspbian-stretch-lite.img`

* Using `raspi-config`:
  * Host name `raspberrypi` (same for all)
  * Wifi network `odysseus_gm`
  * Keyboard layout to FI
  * Timezone to Europe/Helsinki
  * Wifi country to FI
  * Enable SSH server
  * Enable I2C kernel module
  * Enable Remote GPIO (pigpiod)

* `sudo apt-get update` `sudo apt-get dist-upgrade`

* `apt-get install`:
  * pigpio
  * python3-pigpio
  * git
  * python3-pip
  * screen

* `pip3 install python-socketio`

* Install SSH public key

* `git clone https://github.com/OdysseusLarp/odysseus-python.git`

