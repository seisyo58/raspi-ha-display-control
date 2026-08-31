# Raspberry Pi Display Control

Home AssistantからMQTT経由でRaspberry PiのWaylandディスプレイ出力をON/OFFするための小さなブリッジです。

Pi上のMQTTブリッジがコマンドを受信し、`wlr-randr`でディスプレイ出力を操作します。操作に成功した場合だけ状態をMQTTへ通知するため、Home Assistantには最後に成功した状態が表示されます。

## Features

- Home AssistantのMQTT switchからディスプレイをON/OFF
- MQTTの自動再接続とLWTによるavailability通知
- Wayland起動待ちと`wlr-randr`の再試行
- コマンドの排他制御と不正payloadの拒否
- 出力名の明示指定（例：`HDMI-A-1`、`DP-1`）
- 物理出力が1つの場合の出力名自動検出（`DISPLAY_OUTPUT=auto`）
- Raspberry Pi側に時刻スケジュールを持たず、制御をHome Assistantへ集約

## Requirements

- Raspberry PiなどのLinux環境
- Wayland compositor
- `/usr/bin/wlr-randr`
- `flock`を含む`util-linux`
- Python 3.10以降
- `paho-mqtt` 1.6以降
- MQTT broker
- Home Assistant MQTT integration

現在確認している実機環境はRaspberry Pi 3、Raspbian GNU/Linux 13、Wayland/labwcです。

## Quick start

### 1. Piへ配置

リポジトリをPi上へ配置し、必要なパッケージをインストールします。

```sh
sudo apt install python3-paho-mqtt util-linux
sudo ./scripts/install.sh
```

### 2. MQTT接続情報を設定

設定ファイルを編集し、brokerの接続情報を設定します。

```sh
sudoedit /etc/raspi-ha-display-control/mqtt.env
sudo chown pi:pi /etc/raspi-ha-display-control/mqtt.env
sudo chmod 600 /etc/raspi-ha-display-control/mqtt.env
```

`DISPLAY_OUTPUT`には実際の`wlr-randr`出力名を設定します。現在の実機では`HDMI-A-1`です。ディスプレイ出力が1つだけの環境では`auto`も利用できます。

### 3. user serviceを起動

Waylandセッションを所有する`pi`ユーザーで実行します。

```sh
systemctl --user daemon-reload
systemctl --user enable --now raspi-display-mqtt.service
systemctl --user status raspi-display-mqtt.service
```

ログは次のコマンドで確認できます。

```sh
journalctl --user -u raspi-display-mqtt.service -f
```

### 4. Home Assistantへ登録

[ha/mqtt-switch.yaml](ha/mqtt-switch.yaml)をMQTT integrationの設定へ追加します。夜間のON/OFFとPi再起動後の状態再送が必要な場合は、[ha/automations.yaml](ha/automations.yaml)も利用してください。

MQTT command topicはretainしません。Pi再起動後の復旧は、Home Assistantがavailabilityの`online`を検知して現在の状態を再送します。

## Configuration

設定ファイルの雛形は[config/mqtt.env.example](config/mqtt.env.example)です。

主な設定項目は次のとおりです。

| Variable | Description | Example |
| --- | --- | --- |
| `MQTT_HOST` | MQTT brokerのホスト名 | `mqtt.example.local` |
| `MQTT_PORT` | MQTT brokerのポート | `1883` |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | broker認証情報 | — |
| `MQTT_TLS` | TLSを有効化 | `false` |
| `DISPLAY_OUTPUT` | 操作対象の出力名、または`auto` | `HDMI-A-1` |
| `DISPLAY_STATUS_INTERVAL` | 状態再確認の間隔（秒） | `300` |

パスワードなどの秘密情報はGit管理対象へ追加しないでください。実際の設定ファイルは`/etc/raspi-ha-display-control/mqtt.env`に置き、権限を`600`にします。

## Manual control

Pi上で共通ヘルパーを直接実行できます。

```sh
/opt/raspi-ha-display-control/bin/display-control status
/opt/raspi-ha-display-control/bin/display-control on
/opt/raspi-ha-display-control/bin/display-control off
```

MQTTから直接操作する場合は、command topicをretainなしで発行します。

```sh
mosquitto_pub -h MQTT_BROKER -u MQTT_USER -P MQTT_PASSWORD \
  -t home/raspi3/display/hdmi/set -m ON -q 1
```

## Development and tests

ローカル環境では、Piへ接続せずに構文チェックとテストを実行できます。

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile mqtt/display-mqtt-bridge.py tests/test_bridge.py
sh -n bin/display-control scripts/install.sh
sh tests/test_display_control.sh
git diff --check
```

## Project structure

```text
bin/display-control                # Wayland出力の共通ヘルパー
mqtt/display-mqtt-bridge.py        # MQTTブリッジ
systemd/raspi-display-mqtt.service # systemd user service
config/mqtt.env.example             # 設定ファイルの雛形
ha/                                 # Home Assistant設定例
tests/                              # 単体テストとシェルテスト
docs/design.md                      # 詳細な設計と運用方針
```

## Troubleshooting

まずサービスログとディスプレイ状態を確認します。

```sh
journalctl --user -u raspi-display-mqtt.service -n 100 --no-pager
XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 \
  /opt/raspi-ha-display-control/bin/display-control status
```

出力が複数ある場合、`DISPLAY_OUTPUT=auto`は安全のため操作を実行しません。`wlr-randr`で正しい出力名を確認し、`mqtt.env`へ明示指定してください。

## Scope and limitations

このプロジェクトはディスプレイ出力の表示・非表示だけを扱います。Raspberry Piの電源断・再起動、ディスプレイ本体の電源制御、解像度や回転の変更は対象外です。

Pi側にcronやsystemd timerによる時刻制御を追加せず、スケジュールはHome Assistant側で管理してください。

詳細なMQTT仕様、エラー時の扱い、受け入れ条件は[docs/design.md](docs/design.md)を参照してください。
