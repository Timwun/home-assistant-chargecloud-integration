[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

![chargecloud_logo](https://brands.home-assistant.io/_/chargecloud/logo@2x.png)

Chargecloud.de integration
==========================

[Chargecloud.de](https://www.chargecloud.de/) offers software for public EV chargers.
It used by [ladenetz.de](https://ladenetz.de/) and other charging networks in europe.

This integration reads real-time availability status of public EV chargers from chargecloud.de's api.
To install, simply use HACS. Then add an integration and select chargecloud. It will ask you for the EVSE-ID you want to monitor.

Each configured EVSE creates its own status sensor. EVSEs from the same provider
and Chargecloud location belong to one station config entry and one Home
Assistant device; address and proximity are used as fallbacks. Add each port you
want to monitor separately: a later EVSE at the same station is added to its
existing entry. The station options let you split selected ports into separate
devices or merge legacy duplicate station entries.
