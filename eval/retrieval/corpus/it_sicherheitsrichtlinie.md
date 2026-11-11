# Halvorsen Antriebstechnik GmbH
## Konzernweite IT-Sicherheits- und Informationsschutzrichtlinie
**Dokumentencode:** HAT-SEC-POL-2025-V1  
**Geltungsbereich:** Alle Standorte, Gesellschaften und Niederlassungen (Bielefeld, Linz sowie weltweite Vertriebsbüros)  
**Klassifizierungsstufe:** Intern (Vertraulichkeitsstufe C2 nach HAT-Standard)  
**Gültig ab:** 01. Januar 2025  
**Version:** 4.0 (Vollständige Überarbeitung)  
**Verantwortlich:** Chief Information Security Officer (CISO) & IT-Infrastrukturleitung  

---

### 1. Geltungsbereich, Leitbild und Informationssicherheits-Ziele
Die Halvorsen Antriebstechnik GmbH entwickelt, fertigt und vertreibt hochpräzise Antriebslösungen, Planeten- und Kegelradgetriebe sowie integrierte Servomotoren für industrielle Schlüsselbranchen. Der Schutz unseres geistigen Eigentums, unserer Fertigungsdaten, Kundeninformationen und IT-Systeme vor unberechtigtem Zugriff, Spionage, Datenverlust, Sabotage und Betriebsausfällen ist für den Fortbestand unseres Unternehmens von existentieller Bedeutung.

Diese Richtlinie definiert die verbindlichen Sicherheitsstandards für die Nutzung sämtlicher IT-Ressourcen, Kommunikationsmedien und Netzinfrastrukturen an den Standorten Bielefeld und Linz. Sie gilt ausnahmslos für alle Beschäftigten (Festangestellte, Auszubildende, duale Studenten, Werkstudenten), Organmitglieder sowie für externe Dienstleister, Leiharbeitnehmer und Berater, die Zugang zu Informationssystemen der Halvorsen Antriebstechnik GmbH erhalten. Jeder Benutzer ist persönlich dafür verantwortlich, dass die in dieser Richtlinie festgelegten Vorgaben in seinem Arbeitsalltag strikt beachtet und eingehalten werden. Vorgesetzte haben eine Vorbildfunktion und stellen sicher, dass alle Mitarbeiter ihres Verantwortungsbereichs diese Richtlinie kennen und umsetzen.

Die strategischen Hauptziele unserer Informationssicherheit umfassen:
- **Vertraulichkeit:** Schutzbedürftige Forschungs-, Entwicklungs-, Kunden- und Finanzdaten sind vor der unbefugten Kenntnisnahme durch interne und externe Dritte zuverlässig zu schützen.
- **Integrität:** Sämtliche Konstruktionszeichnungen, CNC-Steuerungsprogramme, Montageanweisungen, ERP-Bestände und Qualitätsaufzeichnungen müssen vor unbemerkter, unbeabsichtigter oder böswilliger Verfälschung geschützt werden.
- **Verfügbarkeit:** Die kontinuierliche Betriebsbereitschaft unserer ERP-Systeme, Konstruktionsumgebungen (CAD/CAM), Produktionsleitsysteme (MES) und automatisierten Prüffelder muss zur Vermeidung kostspieliger Stillstandszeiten und Lieferverzögerungen jederzeit gewährleistet sein.
- **Authentizität und Nachvollziehbarkeit:** Alle systemischen Transaktionen und sicherheitskritischen Änderungen müssen eindeutig einer handelnden Entität zugeordnet werden können.

### 2. Gesetzliche, vertragliche und regulatorische Anforderungen
Die Halvorsen Antriebstechnik GmbH unterliegt zahlreichen nationalen und internationalen Rechtsvorschriften, darunter der Datenschutz-Grundverordnung (DSGVO), dem Bundesdatenschutzgesetz (BDSG), dem österreichischen Datenschutzgesetz (DSG) sowie den Vorgaben des Gesetzes über das Bundesamt für Sicherheit in der Informationstechnik (BSI-Gesetz). 

Als qualifizierter Tier-1- und Tier-2-Lieferant für Automatisierung, Schienenfahrzeuge und Schwerindustrie unterliegt Halvorsen zudem den hohen Sicherheitsanforderungen des Verbands der Automobilindustrie (VDA ISA / TISAX mit Prüflevel 3) für Prototypenschutz und vertrauliche Entwicklungsdaten. Sämtliche IT- und OT-Systeme sind so zu konfigurieren und zu betreiben, dass sie den branchenüblichen Standards nach ISO/IEC 27001 (Informationssicherheits-Managementsysteme) und IEC 62443 (Sicherheit für industrielle Automatisierungssysteme) genügen. Bei etwaigen Zielkonflikten zwischen betrieblicher Bequemlichkeit und sicherheitsrelevanten Compliance-Vorgaben hat der Schutz der Systeme und Daten ausnahmslos Vorrang.

### 3. Benutzerkonten, Rollen und Identitätsmanagement
Der Zugang zu IT-Systemen der Halvorsen Antriebstechnik GmbH erfolgt grundsätzlich nach dem Least-Privilege-Prinzip (Prinzip der geringsten Rechtevergabe). Das bedeutet im Einzelnen:
- Jedem Benutzer werden ausschließlich diejenigen Berechtigungen erteilt, die für die ordnungsgemäße Erfüllung seiner konkreten Arbeitsaufgaben zwingend erforderlich sind.
- Die Einrichtung, Änderung und Löschung von Benutzerkonten erfolgt zentral über das Identity- & Access-Management-System (IAM) und bedarf immer der formellen Freigabe durch die zuständige disziplinarische Führungskraft sowie des Datenverantwortlichen (Data Owner).
- Benutzerkonten sind personengebunden und dürfen keinesfalls geteilt, übertragen oder gemeinsam genutzt werden. Sogenannte Sammelbenutzer oder generische Konten sind in Administrations- und Büroumgebungen verboten. In Fertigungsbereichen, in denen Schichtarbeiter an Terminals arbeiten, müssen rollenbasierte Schichtkonten mit technisch restriktiv eingeschränkten Rechten verwendet werden.
- Scheidet ein Mitarbeiter aus dem Unternehmen aus oder wechselt die Abteilung, ist die IT-Abteilung unverzüglich, spätestens jedoch fünf Werktage vor dem Wirksamwerden, durch die Personalabteilung zu informieren, um Zugänge zu sperren bzw. anzupassen. Bei fristlosen Kündigungen erfolgt die Deaktivierung aller Accounts mit sofortiger Wirkung.
- Mindestens halbjährlich wird durch die IT-Infrastrukturleitung ein Rezertifizierungsprozess durchgeführt, bei dem Führungskräfte alle aktiven Berechtigungen ihrer Mitarbeiter auf Notwendigkeit überprüfen und bestätigen müssen.

### 4. Passwortrichtlinie, Authentifizierungsfaktoren und Tokens
Passwörter stellen eine fundamentale Barriere gegen unberechtigten Zugriff dar. Für alle Benutzerkonten im Halvorsen-Netzwerk gelten verbindlich folgende Mindestanforderungen:
- Passwörter müssen eine Mindestlänge von mindestens 14 Zeichen aufweisen.
- Sie müssen Zeichen aus mindestens drei der vier Zeichengruppen enthalten: Großbuchstaben (A-Z), Kleinbuchstaben (a-z), Ziffern (0-9) und Sonderzeichen (!?@#$%&*+-).
- Passwörter dürfen keine leicht erratbaren Begriffe, Wörterbucheinträge, Namen des Benutzers, des Unternehmens, der Familie, Fahrzeugkennzeichen oder einfache Zahlenfolgen enthalten.
- Für privilegierte Konten (z. B. Domänen-Administratoren, Cloud-Tenants, ERP- und Datenbank-Administratoren) beträgt die Mindestpasswortlänge mindestens 18 Zeichen.
- Die Wiederverwendung der letzten 12 Passwörter wird systemseitig technisch unterbunden.
- Multi-Faktor-Authentifizierung (MFA): Für alle Zugriffe von außerhalb des Unternehmensnetzwerks (VPN, Cloud-Dienste wie Microsoft 365, Webmail) sowie für sämtliche administrativen Zugriffe im internen Netz ist die Nutzung eines zweiten Authentifizierungsfaktors (Hardware-Token FIDO2 oder zertifizierte Authenticator-App auf dem Dienst-Smartphone) zwingend vorgeschrieben. SMS-basierte TAN-Verfahren sind wegen Anfälligkeit für SIM-Swapping untersagt.

### 5. Arbeitsplatzsicherheit, Clean-Desk-Policy und Bildschirmsperre
Jeder Beschäftigte hat an seinem physischen Arbeitsplatz dafür Sorge zu tragen, dass unbefugte Dritte keine Einsicht in vertrauliche Informationen nehmen können:
- **Clean-Desk-Policy:** Am Ende des Arbeitstages oder bei Verlassen des Büros für mehr als 30 Minuten sind sämtliche vertraulichen Unterlagen, Akten, Konstruktionszeichnungen und Datenträger in verschließbaren Schränken oder Rollcontainern wegzuschließen. Schlüssel sind sicher zu verwahren und dürfen nicht im Schloss verbleiben.
- **Bildschirmsperre:** Beim Verlassen des Arbeitsplatzes (auch für kurze Kaffeepausen oder Besprechungen) ist der Bildschirm des Computers sofort manuell zu sperren (Tastenkombination Windows-Taste + L bzw. über Menüabmeldung). Auf allen Endgeräten ist eine automatisierte Bildschirmsperre nach maximal 5 Minuten Inaktivität zentral erzwungen.
- **Clean-Screen-Policy:** Monitore an Arbeitsplätzen mit Publikumsverkehr, in Erdgeschossbüros oder in der Nähe von Fenstern sind durch Blickschutzfilter abzusichern, um Shoulder-Surfing zu verhindern. Whiteboards und Flipcharts mit Entwürfen oder Projektnotizen sind nach Besprechungsende gründlich zu löschen.

### 6. Netzsicherheit, Segmentierung und Remote Access (VPN)
Das Unternehmensnetzwerk der Halvorsen Antriebstechnik GmbH ist strikt in logische Sicherheitszonen unterteilt. Es existieren separate Segmente für:
- Büro- und Verwaltungskommunikation (Office-Netzwerk Bielefeld und Linz)
- Entwicklungsumgebungen und Prüfstände (R&D-Netzwerk)
- Produktionsnetzwerke (Operational Technology / OT-Netzwerk für CNC-Maschinen, SPS-Steuerungen und Montageautomaten)
- Rechenzentrum und Virtualisierungsumgebungen (Server-DMZ)
- Gäste- und Besucherzone (Guest-Netzwerk)

Übergänge zwischen diesen Netzsegmenten werden durch hochverfügbare Next-Generation Firewalls überwacht. Ein direkter Datenverkehr aus dem Office-Netz oder gar dem Internet in das Produktionsnetzwerk ist strengstens untersagt und technisch blockiert. Sämtliche Kommunikation in das Fertigungsnetz erfolgt ausschließlich über jump hosts mit Protokollierung.

Fernzugriffe (Remote Access) aus dem Homeoffice oder von Dienstreisen dürfen ausnahmslos nur über den zentralen Unternehmens-VPN-Tunnel mit aktivierter Multi-Faktor-Authentifizierung und kontinuierlichem Endpoint-Compliance-Check erfolgen. Geräte, deren Virenscanner nicht aktuell ist oder deren Betriebssystem ausstehende Sicherheitsupdates aufweist, werden vom VPN-Gateway automatisch blockiert und in ein Quarantäne-Netzwerk geleitet.

### 7. Mobile Endgeräte, Bring-Your-Own-Device (BYOD) und Speichermedien
- Nur von der HAT-IT ausgegebene und im zentralen Mobile-Device-Management (MDM) registrierte Notebooks und Smartphones dürfen mit dem Unternehmensnetzwerk verbunden werden.
- Bring-Your-Own-Device (BYOD): Die Nutzung privater Endgeräte für geschäftliche Aufgaben oder die Speicherung von Unternehmensdaten auf privaten PCs, Tablets oder Smartphones ist grundsätzlich verboten.
- Externe Speichermedien (USB-Sticks, externe SSDs, Speicherkarten): Die USB-Schnittstellen an Firmen-Notebooks und Desktop-Rechnern sind standardmäßig für Massenspeicher gesperrt. Sollte aus betrieblichen Gründen der Einsatz eines externen Datenträgers erforderlich sein, darf ausschließlich ein von der IT-Abteilung freigegebener und hardwareverschlüsselter HAT-USB-Stick verwendet werden. Das Anschließen privater Datenträger ist streng untersagt.

### 8. Umgang mit Geschäftsdaten, Klassifizierung und Datensicherung
Alle Informationen innerhalb der Halvorsen Antriebstechnik GmbH sind in eine von vier Schutzklassen eingeteilt:
1. Öffentlich (Public): Für die externe Veröffentlichung freigegebene Broschüren, Datenblätter, Stellenausschreibungen und Pressemitteilungen.
2. Intern (Internal): Betriebsinternes Wissen, Telefonlisten, Arbeitsanweisungen, Standardberichte.
3. Vertraulich (Confidential): Technische Konstruktionsunterlagen, Kundenangebote, Kalkulationen, Lieferantenverträge, Personalakten.
4. Streng Vertraulich (Strictly Confidential): Strategische M&A-Pläne, Vorstandsprotokolle, Patente vor der Einreichung, Quellcodes proprietärer Steuerungsalgorithmen.

Vertrauliche und streng vertrauliche Dokumente dürfen nur auf den zentralen Netzlaufwerken oder freigegebenen SharePoint-Bibliotheken mit aktiviertem Rights-Management abgelegt werden. Eine Speicherung auf dem lokalen Laufwerk C: des Notebooks ist verboten. Die zentralen Datenbestände werden täglich automatisiert gesichert und nach dem 3-2-1-Backup-Prinzip auf redundante Speicher an räumlich getrennten Standorten (Bielefeld und Linz) unveränderbar (WORM-Storage) repliziert.

---

### 9. Erkennung, Klassifizierung und Meldung von Sicherheitsvorfällen (Security Incident Management)
Die frühzeitige Erkennung und unmittelbare Eindämmung von Sicherheitsvorfällen ist entscheidend, um Schäden vom Unternehmen, unseren Kunden und unseren Mitarbeitern abzuwenden. Jeder Verdacht auf eine Beeinträchtigung der Informationssicherheit ist unverzüglich zu melden.

**Was gilt als meldepflichtiger Sicherheitsvorfall?**
- Verdacht auf Schadsoftware-Infektionen (Ransomware, Trojaner, ungewöhnliche Systemverlangsamung, unerklärliche Dateiverschlüsselung oder veränderte Dateiendungen).
- Empfang zielgerichteter Phishing-Mails, verdächtige Anhänge oder die versehentliche Preisgabe von Anmeldedaten auf gefälschten Webseiten.
- Physischer Verlust oder Diebstahl von Firmenendgeräten (Notebook, Smartphone, Tablet), Dienstausweisen, Hardware-Tokens oder vertraulichen Akten.
- Unberechtigte Zugriffsversuche, auffällige Sperrungen von Benutzerkonten oder unerklärliche Fehlermeldungen bei Zugriffsrechten.
- Physisches Eindringen unbekannter Personen in gesicherte Bereiche (Serverräume, Labore, Konstruktionsbüros, Fertigungsinseln).
- Fehlleitungen von vertraulichen Mitteilungen oder Konstruktionsdaten an falsche externe Empfänger.

**Meldewege und Reaktionsfristen:**
Sicherheitsvorfälle müssen binnen maximal 45 Minuten an das Computer Emergency Response Team gemeldet werden. Diese 45-Minuten-Frist beginnt in dem Moment, in dem der Mitarbeiter oder Dienstleister den Verdacht schöpft oder die sicherheitsrelevante Anomalie bemerkt.

Die schriftliche Erstmeldung erfolgt primär per E-Mail an das zentrale Postfach:
- E-Mail: `cert@halvorsen-antrieb.example`

Bei schwerwiegenden Vorfällen der Priorität 1 (z. B. akuter Ausbruch von Verschlüsselungstrojanern, unberechtigter Datenabfluss von Kern-IP, Ausfall von ERP- oder Produktionsleitsystemen im Werk Bielefeld oder Linz, physischer Einbruch in das Rechenzentrum) ist zusätzlich sofort und zu jeder Tages- und Nachtzeit die zentrale IT-Notfall-Hotline anzurufen:
- **Zentrale CERT-Notfall-Hotline (24/7 Rufbereitschaft):** 0521 555-0911

Der Meldende hat folgende Angaben präzise zu übermitteln:
1. Wer meldet (Name, Abteilung, Telefonnummer für Rückfragen)?
2. Was ist genau vorgefallen (Symptome, Fehlermeldungen, Art der Beeinträchtigung)?
3. Welche Systeme, Rechner, Netzwerke oder Datenbanken sind betroffen?
4. Wann trat das Ereignis auf bzw. wann wurde es erstmals beobachtet?
5. Welche Sofortmaßnahmen wurden bisher eigenständig ergriffen?

Wichtige Handlungsanweisung bei Ransomware- oder Hacker-Verdacht: Das betroffene Endgerät darf **nicht** ausgeschaltet oder neu gestartet werden! Ziehen Sie unverzüglich das Netzwerkkabel ab und deaktivieren Sie den physischen WLAN-Schalter bzw. Flugmodus, um das System vom Netzwerk zu trennen. Das Belassen im eingeschalteten Zustand ist zwingend erforderlich, damit forensische Analyseteams flüchtige Beweise im Arbeitsspeicher (RAM) sichern können.

### 10. Gäste-WLAN, temporäre Netzzugänge und Fremdfirmen-Zutritt
Besuchern, Kunden, Wirtschaftsprüfern, Auditoren und externen Handwerkern kann für die Dauer ihres Aufenthalts ein temporärer Internetzugang zur Verfügung gestellt werden.
- Das Gäste-WLAN („HAT-Guest“) ist vollständig vom internen Produktions- und Unternehmensnetzwerk isoliert und verfügt ausschließlich über einen bandbreitenbeschränkten und inhaltsgefilterten direkten Internetzugang.
- Ein Zugriff auf interne Server, Datenbanken, Drucker oder Netzlaufwerke ist über das Gäste-WLAN technisch unmöglich.
- Richtlinie für das Gast-Kennwort: Das Kennwort für das Gäste-WLAN "HAT-Guest" wird jeweils am ersten Werktag eines Monats durch das IT-Helpdesk neu vergeben und an der Pforte hinterlegt. Besucher erhalten die individuellen Zugangsdaten am Empfang im Verwaltungsgebäude Bielefeld bzw. an der Werkspforte in Linz nach persönlicher Registrierung, Vorlage eines gültigen Lichtbildausweises und Gegenzeichnung der Sicherheitsbelehrung für Besucher.
- Fremdfirmen und externe Wartungstechniker, die zur Wartung von Produktionsanlagen Zugriff auf das OT-Netzwerk benötigen, müssen während der gesamten Tätigkeitsdauer von einem autorisierten HAT-Mitarbeiter begleitet werden. Jeder Fernwartungszugang durch externe Maschinenhersteller darf nur über ein zeitlich befristetes und vollständig auditiertes Ticket im Fernwartungs-Gateway freigeschaltet werden. Nach Abschluss der Wartungsarbeiten ist der Fernzugang unverzüglich durch den HAT-Verantwortlichen manuell zu schließen.

### 11. Überwachung, Protokollierung und Datenschutzkonformität
Zur Gewährleistung der Netzsicherheit und zur Abwehr zielgerichteter Cyber-Angriffe setzt die Halvorsen Antriebstechnik GmbH automatisierte Sicherheitsüberwachungswerkzeuge ein. Hierzu gehören Intrusion Detection & Prevention Systeme (IDS/IPS), Security Information and Event Management (SIEM) sowie Endpoint Detection and Response (EDR).
- System-, Firewall- und Authentifizierungsprotokolle werden automatisiert auf verdächtige Anomalien, Privilege Escalation und unberechtigte Datenbewegungen analysiert.
- Eine Leistungs- oder Verhaltenskontrolle der Beschäftigten findet nicht statt. Sämtliche Protokollierungsmaßnahmen erfolgen im strengen Einklang mit den Bestimmungen der gültigen Betriebsvereinbarungen und den gesetzlichen Vorschriften der DSGVO.
- Regelmäßige simulierte Phishing-Kampagnen und Sensibilisierungsmaßnahmen dienen ausschließlich Schulungszwecken und der Schärfung des allgemeinen Sicherheitsbewusstseins im Arbeitsalltag.

### 12. Sanktionen bei Richtlinienverstößen, Überprüfungszyklen und Inkrafttreten
Die Einhaltung dieser IT-Sicherheitsrichtlinie wird regelmäßig durch interne und externe Sicherheitsaudits sowie stichprobenartige Kontrollen überprüft.
- Vorsätzliche oder grob fahrlässige Verstöße gegen diese Richtlinie gefährden die Sicherheit des Unternehmens, den Erhalt von Arbeitsplätzen und das Vertrauen unserer Kunden. Sie können arbeitsrechtliche Konsequenzen (von der formellen Abmahnung bis hin zur fristlosen Kündigung des Arbeitsverhältnisses) sowie zivilrechtliche Schadenersatzforderungen und strafrechtliche Verfolgung nach sich ziehen.
- Diese Richtlinie wird mindestens einmal jährlich durch den CISO überprüft und bei veränderter Bedrohungslage oder regulatorischen Neuerungen unverzüglich angepasst.
- Diese Fassung tritt mit Wirkung zum 01. Januar 2025 in Kraft und ersetzt alle vorherigen IT-Nutzungs- und Sicherheitsrichtlinien der Halvorsen Antriebstechnik GmbH vollumfänglich.

Bielefeld / Linz, den 01. Januar 2025  
*Geschäftsführung & Chief Information Security Officer (CISO)*  
*Halvorsen Antriebstechnik GmbH*
