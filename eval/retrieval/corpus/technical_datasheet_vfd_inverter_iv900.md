# TECHNICAL SPECIFICATION & PRODUCT DATASHEET
## Industrial Variable Frequency Drive (VFD) Inverter Model IV-900
**Document Code:** HAT-TDS-IV900-EN-REV4  
**Manufacturer:** Halvorsen Antriebstechnik GmbH, Electronics Division  
**Publication Date:** January 2025  
**Product Series:** InvertoDrive IV-Industrial High-Dynamic Series  

---

### 1. General Product Overview
The InvertoDrive IV-900 is an advanced microprocessor-controlled variable frequency inverter engineered for precise speed and torque regulation of asynchronous induction motors and permanent-magnet synchronous servomotors (PMSM). Developed for demanding multi-axis machinery, crane hoists, test stands, and extruders, the IV-900 features sensorless vector control (SVC), closed-loop field-oriented control (FOC), and integrated functional safety certified to SIL 3 / PL e.

### 2. Electrical Specifications and Performance Data
- **Rated Output Power (Heavy Duty - 150% Overload for 60 s):** 45 kW
- **Rated Output Power (Normal Duty - 110% Overload for 60 s):** 55 kW
- **Nominal Input Voltage:** 3-phase 380 V to 480 V AC (+/- 10%)
- **Mains Frequency:** 47 to 63 Hz
- **Nominal Output Current (at 400 V AC):** 90.0 A continuous
- **Peak Output Current (3 s stall capacity):** 180.0 A
- **Output Frequency Range:** 0.0 to 599.0 Hz (PWM switching frequencies selectable from 2 kHz to 16 kHz)
- **Efficiency at Rated Load:** > 98.2 %
- **DC Bus Working Voltage:** 510 V to 750 V DC (with internal regenerative surge protection)
- **Braking Resistor Chopper:** Built-in dynamic braking transistor module included as standard equipment across all frame sizes.

### 3. Environmental Specifications and Physical Installation
- **Operating Ambient Temperature:** -20 °C to +55 °C (-4 °F to 131 °F)
- **Derating Rule:** Full rated continuous current is delivered up to +40 °C. Above 40 °C, linear output current derating of 1.5% per °C applies up to the maximum operating temperature of +55 °C.
- **Storage and Transportation Temperature:** -40 °C to +70 °C
- **Relative Humidity:** 5 % to 95 % non-condensing (conformal coated printed circuit boards to class 3C3 according to IEC 60721-3-3)
- **Maximum Operational Altitude:** 1,000 meters above sea level without derating; up to 3,000 m with 1% power reduction per 100 m above 1,000 m.
- **Ingress Protection Class:** IP20 / NEMA 1 (enclosed chassis for central control cabinet installation)
- **Cooling Method:** Forced air convection via temperature-regulated dual ball-bearing fans.

### 4. Communication Interfaces and Software Architecture
- **Onboard Industrial Ethernet:** Integrated 2-port RJ45 Ethernet switch supporting PROFINET IO (RT/IRT conformance class C) and EtherCAT slave profile.
- **Legacy Fieldbus:** Isolated CANopen interface (DS301/DSP402 drive profile) with configurable termination resistor.
- **USB Service Interface:** Type-C service port on front fascia for PC configuration and diagnostic capture.
- **Pre-installed Firmware Version:** Release **v3.4.1** (factory flashed firmware featuring enhanced anti-hunting algorithms for low-inertia PMSM rotors).
- **Integrated Functional Safety:** Safe Torque Off (STO) dual-channel input certified to EN ISO 13849-1 Cat 4 / PL e and IEC 61508 SIL 3.

### 5. Mechanical Dimensions and Weight
- **Frame Size:** Frame 4 (Compact Book-Format)
- **Dimensions (Height x Width x Depth):** 460 mm x 180 mm x 245 mm
- **Net Unit Weight:** 16.4 kg
- **Recommended Minimum Air Clearance:** 100 mm top and bottom, 0 mm side-by-side book-mounting.

### 6. Technical Support and Documentation
Firmware updates, GSDML device description files, and commissioning tools (HAT-DriveStudio) can be accessed via the engineering portal or requested from our electronics support team: Phone +49 521 555-0255 or email `electronics-support@halvorsen-antrieb.example`.
