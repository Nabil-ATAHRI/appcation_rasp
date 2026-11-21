# Passerelle énergie Raspberry Pi

Cette passerelle tourne sur un **Raspberry Pi 5**. Elle :

1. lit les trames JSON envoyées par le STM32 (UART4, 115200 8N1) sur le port
   série du Pi ;
2. valide chaque trame et la conserve dans une file SQLite locale (aucune perte
   si le réseau tombe) ;
3. transmet les mesures par lots à l’API du VPS en HTTPS.

C’est un programme Python autonome lancé par systemd. Malgré le nom du dossier
(`app_rasp_ros2`), **ROS 2 n’est pas nécessaire**. Le dashboard web se trouve
dans le projet `Monitoring_energy`, pas ici.

```
STM32 (UART4) ──série──▶ Raspberry Pi 5 (energy-gateway) ──HTTPS──▶ VPS (API + dashboard)
```

## Contenu du projet

| Chemin                                   | Rôle                                         |
|------------------------------------------|----------------------------------------------|
| `src/energy_gateway/energy_gateway/`     | Code de la passerelle (`gateway.py`, `core.py`) |
| `src/energy_gateway/test/`               | Tests unitaires                              |
| `deploy/energy-gateway.service`          | Service systemd                              |
| `deploy/gateway.env.example`             | Modèle de configuration                      |
| `requirements.txt`                       | Dépendance Python (`pyserial`)               |

Le dossier `src/energy_interfaces/` est un reste de l’ancienne version ROS 2 :
il est vide et n’est pas utilisé.

## Format des trames STM32 → Raspberry Pi

Le STM32 envoie sur UART4 (115200 bauds, 8N1) **une trame JSON par ligne**,
terminée par `\n`, de 2048 octets au maximum. Ordre d’envoi : la trame de
démarrage, puis en boucle les SHT20 (id 1 et 3), puis le MPR47S (id 5).

**Démarrage** (envoyée une fois au reset du STM32) :

```json
{"v":1,"event":"boot"}
```

**SHT20 (température / humidité)** :

```json
{"v":1,"sensor":"sht20","id":1,"seq":435,"uptime_ms":1604176,"status":"ok","error":"","data":{"temperature_c":24.2,"humidity_pct":59.5}}
```

**MPR47S (mesures électriques)** :

```json
{"v":1,"sensor":"mpr47s","id":5,"seq":437,"uptime_ms":1611132,"status":"ok","error":"","data":{"voltage_v":[V1,V2,V3,VN],"line_voltage_v":[V12,V23,V31],"current_a":[I1,I2,I3,IN],"active_power_w":[P1,P2,P3,Ptot],"reactive_power_var":[Q1,Q2,Q3,Qtot],"apparent_power_va":[S1,S2,S3,Stot],"power_factor":[PF1,PF2,PF3],"frequency_hz":49.97}}
```

**Capteur qui ne répond pas** (timeout Modbus, CRC…) :

```json
{"v":1,"sensor":"sht20","id":3,"seq":436,"uptime_ms":1607050,"status":"error","error":"acquisition_failed","data":null}
```

| Champ       | Signification                                                    |
|-------------|------------------------------------------------------------------|
| `v`         | Version du protocole, toujours `1`                               |
| `sensor`    | `sht20` ou `mpr47s`                                              |
| `id`        | Adresse Modbus du capteur (1 à 247)                              |
| `seq`       | Compteur incrémenté à chaque trame ; repart à 0 au reset du STM32 |
| `uptime_ms` | Temps écoulé depuis le démarrage du STM32                        |
| `status`    | `ok` ou `error`                                                  |

La passerelle rejette toute trame qui ne respecte pas exactement ce format
(champ manquant ou en trop, température hors de −40…125 °C, humidité hors de
0…100 %, fréquence hors de 0…1000 Hz). Les trames d’erreur sont transmises au
VPS comme les autres. Un saut de `seq` est signalé dans les journaux
(`STM32 sequence gap`) ; un retour en arrière démarre une nouvelle session.

## Installation sur le Raspberry Pi 5

Effectuez les étapes dans l’ordre. Les commandes supposent que le projet a été
copié dans `~/NABIL_ws/app_rasp_ros2` sur le Raspberry Pi.

### 1. Câbler le STM32 au Raspberry Pi

Les deux cartes fonctionnent en 3,3 V : aucun adaptateur de niveau n’est
nécessaire.

| STM32F407 (UART4) | Raspberry Pi 5                |
|-------------------|-------------------------------|
| PC10 (TX)         | GPIO15 / RXD, broche 10       |
| PA1 (RX)          | GPIO14 / TXD, broche 8        |
| GND               | GND, broche 6                 |

Le TX d’une carte va sur le RX de l’autre. La masse commune est obligatoire.

### 2. Activer l’UART des broches GPIO

Sur le Pi 5, l’UART des GPIO 14/15 s’appelle `/dev/ttyAMA0` et il est
**désactivé par défaut**. `/dev/ttyAMA10` correspond au petit connecteur
« UART debug » à 3 broches situé entre les deux micro-HDMI : il ne reçoit rien
de ce qui arrive sur les broches GPIO.

Activez l’UART et retirez la console série qui l’occuperait :

```bash
grep -q '^dtparam=uart0=on' /boot/firmware/config.txt || \
  echo 'dtparam=uart0=on' | sudo tee -a /boot/firmware/config.txt
# Ubuntu (os_prefix=current/ dans config.txt) : le cmdline actif est dans current/
for f in /boot/firmware/cmdline.txt /boot/firmware/current/cmdline.txt; do
  [ -f "$f" ] && sudo sed -i 's/console=serial0,[0-9]* //; s/console=ttyAMA0,[0-9]* //' "$f"
done
sudo systemctl mask serial-getty@ttyAMA0.service
sudo reboot
```

Après le redémarrage, vérifiez que les trames arrivent :

```bash
ls -l /dev/ttyAMA0
stty -F /dev/ttyAMA0 115200 raw -echo && cat /dev/ttyAMA0
```

Des lignes JSON comme celle-ci doivent s’afficher toutes les quelques
secondes :

```json
{"v":1,"sensor":"sht20","id":1,"seq":435,"uptime_ms":1604176,"status":"ok","error":"","data":{"temperature_c":24.2,"humidity_pct":59.5}}
```

Quittez avec `Ctrl+C`. Si rien n’apparaît, ne continuez pas : voir
[Dépannage](#dépannage).

### 3. Copier le projet dans `/opt/energy`

```bash
sudo mkdir -p /opt/energy
sudo cp -a ~/NABIL_ws/app_rasp_ros2 /opt/energy/
ls /opt/energy/app_rasp_ros2
```

La dernière commande doit afficher `README.md`, `deploy`, `src` et
`requirements.txt`.

### 4. Installer Python et créer l’utilisateur du service

```bash
sudo apt update
sudo apt install -y python3 python3-serial
id energy >/dev/null 2>&1 || sudo useradd --system --user-group --home-dir /var/lib/energy-gateway energy
sudo usermod -aG dialout energy
```

`useradd` crée le compte `energy` seulement s’il n’existe pas encore. Le groupe
`dialout` donne au service l’accès au port série.

### 5. Installer et remplir la configuration

```bash
sudo cp /opt/energy/app_rasp_ros2/deploy/gateway.env.example /etc/energy-gateway.env
sudo cp /opt/energy/app_rasp_ros2/deploy/energy-gateway.service /etc/systemd/system/
sudoedit /etc/energy-gateway.env
```

Remplacez les exemples par vos valeurs réelles :

```ini
GATEWAY_ID=raspberry-01
GATEWAY_TOKEN=REMPLACER_PAR_LE_JETON_CONFIGURE_SUR_LE_VPS
CLOUD_URL=https://VOTRE_DOMAINE/api/v1/telemetry/batch
OUTBOX_PATH=/var/lib/energy-gateway/outbox.sqlite3
```

- `GATEWAY_ID` : identifiant unique de la passerelle (lettres, chiffres, `_`
  et `-`, 64 caractères maximum). Il doit correspondre à celui déclaré sur le
  VPS.
- `GATEWAY_TOKEN` : le secret associé à cette passerelle dans `GATEWAY_TOKENS`
  sur le VPS. Au moins 32 caractères, sans espace. Gardez-le privé.
- `CLOUD_URL` : URL **HTTPS** de l’API du VPS. L’URL HTTP locale du dashboard
  est refusée.
- `OUTBOX_PATH` : file SQLite locale. Gardez la valeur ci-dessus.

Le service lit `/dev/ttyAMA0` par défaut. Si vous utilisez un adaptateur
USB-série à la place, repérez son nom stable avec `ls -l /dev/serial/by-id/`,
puis remplacez `/dev/ttyAMA0` dans la ligne `ExecStart` de
`/etc/systemd/system/energy-gateway.service`. Évitez `/dev/ttyUSB0` ou
`/dev/ttyACM0`, qui peuvent changer après un redémarrage.

### 6. Démarrer le service

```bash
sudo chmod 600 /etc/energy-gateway.env
sudo systemctl daemon-reload
sudo systemctl enable --now energy-gateway
```

### 7. Vérifier le fonctionnement

```bash
sudo systemctl status energy-gateway --no-pager
sudo journalctl -fu energy-gateway
```

Avec les journaux ouverts, redémarrez le STM32 :

- `Connected to STM32 on /dev/ttyAMA0` : le port série est ouvert (cela ne
  prouve pas encore la réception).
- `STM32 boot: new acquisition session` : la trame de démarrage du STM32 a bien
  été reçue.
- Aucun message `Cloud forwarding failed` : les mesures partent vers le VPS.

## Mettre à jour la passerelle

Après avoir recopié la nouvelle version dans `~/NABIL_ws/app_rasp_ros2` :

```bash
sudo cp -a ~/NABIL_ws/app_rasp_ros2/. /opt/energy/app_rasp_ros2/
sudo cp /opt/energy/app_rasp_ros2/deploy/energy-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart energy-gateway
```

`/etc/energy-gateway.env` n’est pas écrasé.

## Dépannage

| Symptôme dans `journalctl`                       | Cause probable et solution |
|--------------------------------------------------|----------------------------|
| `Cannot open /dev/ttyAMA0` (`Permission denied`) | Console série encore active : `cat /proc/cmdline` ne doit pas contenir `console=ttyAMA0` ; refaire l’étape 2. Sinon UART non activé ou utilisateur `energy` hors du groupe `dialout`. |
| `Connected to STM32` mais aucune trame           | Mauvais port (`ttyAMA10` au lieu de `ttyAMA0`), TX/RX inversés, masse absente, ou console série encore active dans `cmdline.txt`. |
| `Malformed or oversized STM32 frame discarded`   | Débit différent de 115200 ou parasites sur la ligne. |
| `Invalid STM32 frame: ...`                       | Trame reçue mais format inattendu : comparer avec le firmware STM32. |
| `STM32 sequence gap: a -> b`                     | Trames perdues sur la liaison série (câble trop long, parasites). |
| `Cloud forwarding failed; records retained`      | URL, jeton ou réseau. Les mesures restent dans la file SQLite et seront renvoyées. |
| `Gateway initialization failed`                  | Variable manquante ou invalide dans `/etc/energy-gateway.env`. |

Pour lire le port série directement, arrêtez d’abord le service (un seul
programme peut lire le port à la fois) :

```bash
sudo systemctl stop energy-gateway
python3 -m serial.tools.miniterm /dev/ttyAMA0 115200
# Ctrl+] pour quitter
sudo systemctl start energy-gateway
```

Côté STM32, le terminal de diagnostic (USART3, 9600 8N1) affiche chaque trame
envoyée (`UART4 TX (n bytes)`). Si les trames y apparaissent mais pas sur le
Pi, le problème est dans le câblage ou la configuration du port série du Pi.

## Développement et tests locaux

Depuis la racine du projet :

```bash
python3 -m pip install -r requirements.txt pytest
PYTHONPATH=src/energy_gateway python3 -m pytest src/energy_gateway/test
```

Lancement manuel, sans systemd :

```bash
export GATEWAY_ID=raspberry-01
export GATEWAY_TOKEN='VOTRE_JETON_CONFIGURE_SUR_LE_VPS'
export CLOUD_URL='https://VOTRE_DOMAINE/api/v1/telemetry/batch'
export OUTBOX_PATH=/tmp/energy-gateway-outbox.sqlite3
PYTHONPATH=src/energy_gateway python3 -m energy_gateway.gateway \
  --serial-port=/dev/ttyAMA0 --baudrate=115200
```

Ajoutez `LOG_LEVEL=DEBUG` pour voir chaque mesure enregistrée.
