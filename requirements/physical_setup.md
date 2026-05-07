# Hardware Package & Physical Deployment Guide

## 1. Bill of Materials (BOM)

| Category | Item | Specification | Notes |
| :--- | :--- | :--- | :--- |
| Camera | Axis P1455-LE | 1080p, PoE, Rugged | Primary sensor |
| Mount | Axis T91 Wall/Pole | Dedicated Axis mount | Stainless steel straps for pole attachment |
| Internet | Starlink Ethernet Adapter | Proprietary RJ45 adapter | Standard Starlink Gen 2 has no Ethernet ports without this |
| Power/Net | TP-Link TL-SG1005P | 4-Port PoE+ Switch | Supplies power to camera via data cable |
| Storage | MicroSD Card | 128GB+ High-Endurance | Edge storage for Starlink outages |
| Cabling | Cat6 Shielded (SFTP) | 20m–50m Outdoor Rated | Must be shielded for high-pole static bleed |
| Protection | IP66 Enclosure | 300×300×150 mm | Houses switch and power supply at pole base |
| Surge | PoE Surge Protector | Outdoor-rated RJ45 | Protects switch from pole static/lightning |

## 2. Physical Signal Path

```
Starlink Dish
  → (proprietary cable) → Starlink Router
  → Starlink Ethernet Adapter → TP-Link Switch (inside IP66 enclosure)
  → PoE Surge Protector → Shielded Cat6 (up the pole)
  → Axis P1455-LE
```

## 3. Physical Constraints

### Grounding
The camera pole acts as a lightning rod. The ground terminal of the PoE Surge Protector MUST be connected to a copper earth rod or the building's grounding system.

### Enclosure Thermal Management
The TP-Link switch and power supply generate heat. The enclosure MUST NOT be packed with insulation. Mount in shade or use a white enclosure to reduce solar heat gain.

### Shielded Continuity
Shielded RJ45 connectors MUST be used at both ends. The drain wire inside the SFTP cable MUST make contact with the metal casing of the RJ45 connector to bleed off static.

### Drip Loops
At the camera head and enclosure entry point, leave at least 150mm (6 inches) of cable slack hanging below the entry point. This prevents water from wicking along the cable into the electronics.

### Cable Entry
All enclosure cable entries MUST use IP-rated cable glands. Do not leave open holes.
