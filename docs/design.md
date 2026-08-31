# Raspberry Pi 画面制御（Home Assistant + MQTT）

Home Assistant（HA）から MQTT で Raspberry Pi の HDMI 出力を ON/OFF するための設計メモです。

このリポジトリでは、Pi 上の Wayland セッションに対して `wlr-randr` を実行し、画面の表示・非表示を切り替えます。HA と Pi の間は MQTT で接続し、HA のスイッチに現在状態も反映します。

## 現在の前提

実機で確認済みの前提は次のとおりです。

| 項目 | 値 |
| --- | --- |
| 対象 | Raspberry Pi 3（ホスト名 `pi3`） |
| OS | Raspbian GNU/Linux 13（trixie） |
| デスクトップ | Wayland / labwc |
| HDMI 出力 | `HDMI-A-1` |
| Wayland ソケット | `/run/user/1000/wayland-0` |
| 操作用コマンド | `/usr/bin/wlr-randr` |
| タイムゾーン | `Asia/Tokyo` |
| 実行ユーザー | `pi` |

画面操作に必要な環境変数は、MQTT ブリッジから実行する場合も明示します。

```sh
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-0
```

基本操作は次のとおりです。

```sh
wlr-randr --output HDMI-A-1 --off
wlr-randr --output HDMI-A-1 --on
```

OFF にした後の `wlr-randr` の表示では、Wayland compositor 側の都合で対象出力が `NOOP-1` と表示されることがあります。したがって、状態確認は HDMI コネクタ名の存在だけで判断せず、操作結果と再確認結果を組み合わせます。

## 目的と範囲

### 目的

- HA のスイッチから Pi の HDMI 画面を ON/OFF する
- Pi で実行した結果を HA に返し、スイッチの状態を同期する
- Pi の再起動、Wayland の起動遅延、MQTT broker の一時停止から自動復旧する
- 夜間の ON/OFF を含め、すべての制御判断を HA に集約する

### 今回の対象外

- Raspberry Pi の電源断・再起動
- HDMI ケーブルやディスプレイの電源制御
- 画面解像度・回転・レイアウトの変更
- HA 以外のクライアント向けの汎用ディスプレイ管理

## 採用する構成

```text
┌──────────────────────┐       MQTT        ┌────────────────────────┐
│ Home Assistant       │ ────────────────▶ │ Raspberry Pi 3          │
│ MQTT switch          │  set: ON / OFF   │ MQTT display bridge     │
│                      │ ◀──────────────── │                          │
└──────────────────────┘ state/availability └───────────┬────────────┘
                                                         │
                                                         │ 共通ヘルパー
                                                         ▼
                                             XDG_RUNTIME_DIR +
                                             WAYLAND_DISPLAY
                                                         │
                                                         ▼
                                             wlr-randr HDMI-A-1
```

### 役割分担

1. HA は `ON` / `OFF` を command topic に発行する。
2. Pi の MQTT ブリッジは payload を厳密に検証する。
3. ブリッジは排他制御付きの共通ヘルパーを呼び出す。
4. 共通ヘルパーが Wayland 環境を設定し、`wlr-randr` を実行する。
5. 成功時は Pi が `ON` / `OFF` を state topic に retain 付きで発行する。
6. MQTT 接続状態は availability topic で `online` / `offline` を通知する。

画面の操作経路は MQTT ブリッジから共通ヘルパーへ一本化します。Pi 側ではスケジュールや自動判定を行わず、HA から受けた ON/OFF だけを実行します。

## MQTT トピック仕様

トピックの prefix は `home/raspi3/display` とします。

| 用途 | トピック | payload | retain |
| --- | --- | --- | --- |
| 操作要求 | `home/raspi3/display/hdmi/set` | `ON` / `OFF` | **false** |
| 現在状態 | `home/raspi3/display/hdmi/state` | `ON` / `OFF` | **true** |
| 接続状態 | `home/raspi3/display/availability` | `online` / `offline` | online は true、LWT は true |
| エラー通知（任意） | `home/raspi3/display/error` | 人間が読める短いメッセージ | false |

### retain 方針

- `set` は retain しない。Pi や MQTT broker の再起動後に、過去の命令を再実行させないためです。
- `state` は retain する。HA の再起動後も最後に成功した状態を表示するためです。
- `availability` は retained LWT を使い、ブリッジ停止時に HA が unavailable と判断できるようにします。

### QoS

通常運用では QoS 1 を使用します。ON/OFF は冪等な操作なので、同じ命令を複数回受信しても問題が起きないようにします。ブリッジ側には短時間の連続命令を直列化するロックを設けます。

## Home Assistant 側の定義

MQTT integration の設定例です。HA の UI で MQTT エンティティを作成する場合も、次の値に合わせます。

```yaml
mqtt:
  - switch:
      name: "ラズパイ画面"
      unique_id: raspi3_hdmi_display
      command_topic: "home/raspi3/display/hdmi/set"
      state_topic: "home/raspi3/display/hdmi/state"
      availability_topic: "home/raspi3/display/availability"
      payload_on: "ON"
      payload_off: "OFF"
      state_on: "ON"
      state_off: "OFF"
      payload_available: "online"
      payload_not_available: "offline"
      qos: 1
      retain: false
```

`retain: false` は HA が発行する command message に対する設定です。Pi が発行する state message の retain とは別の設定です。

HA 側では、`state_topic` を設定して command topic と状態を分離します。Pi が操作に失敗したときは、最後に成功した state を不用意に変更しません。ブリッジが停止した場合は availability が `offline` になり、HA から切断状態を確認できます。

## Raspberry Pi 側の構成

実装時のファイル構成は次を想定します。

```text
/opt/raspi-ha-display-control/
├── bin/
│   └── display-control              # ON/OFF の共通ヘルパー
└── mqtt/
    └── display-mqtt-bridge.py      # MQTT subscribe / publish

/etc/raspi-ha-display-control/
└── mqtt.env                         # broker 接続情報（権限 600）

/etc/systemd/user/
└── raspi-display-mqtt.service       # pi ユーザーの user service
```

### 共通ヘルパー

`display-control` は次の引数を受け付けます。

| 引数 | 動作 |
| --- | --- |
| `on` | 設定された出力を ON にする |
| `off` | 設定された出力を OFF にする |
| `status` | 現在の状態を調べ、`ON` / `OFF` / `UNKNOWN` を返す（診断用） |

共通ヘルパーの責務は次のとおりです。

- `/usr/bin/wlr-randr` を絶対パスで呼ぶ
- `XDG_RUNTIME_DIR=/run/user/1000` と `WAYLAND_DISPLAY=wayland-0` を設定する
- Wayland がまだ起動していない場合は、一定回数・一定間隔で再試行する
- `flock` などで操作を直列化する
- 不正な引数を拒否し、対象出力名を固定する
- 成功・失敗を終了コードで呼び出し元に返す
- 時刻による ON/OFF 判定は行わない。タイムゾーンは HA 側の自動化で管理する

対象出力は `DISPLAY_OUTPUT` で指定します。`HDMI-A-1`、`HDMI-A-2`、`DP-1` などの実際の `wlr-randr` 出力名を設定できます。`auto` を指定した場合は、物理ディスプレイ出力が1つだけのときに自動選択し、選択した名前を `/run/user/1000/raspi-ha-display-control.output` に保存します。出力が複数ある場合は誤操作を避けるため `UNKNOWN` / 失敗とし、明示設定を要求します。出力OFF後に compositor が `NOOP-1` と表示しても、保存した物理出力名を使って再度ONにできます。

### MQTT ブリッジ

ブリッジは `pi` ユーザーの systemd user service として常駐させます。

- MQTT broker に接続したら `availability=online` を発行する
- 切断時は自動再接続する
- 接続時に retained command を取得・実行しない
- `ON` / `OFF` 以外の payload は無視してログに残す
- 受信した命令を一度に一つずつ処理する
- 成功したら state を発行する
- 失敗したら state は変更せず、ログと任意の error topic に記録する
- 起動時と定期的な再確認時に `status` を実行し、state を再発行する

systemd の起動は Wayland セッション開始後を基本としますが、起動順序だけに依存しません。ブリッジまたは共通ヘルパーに Wayland ソケットの準備待ちと再試行を持たせます。

### 接続情報

broker のホスト名、ポート、ユーザー名、パスワード、TLS 設定はコードや Git 管理対象に含めません。`/etc/raspi-ha-display-control/mqtt.env` に分離し、`pi` 以外が読めない権限にします。

MQTT broker 側では、少なくとも次の ACL を設定します。

- Pi のユーザーは `home/raspi3/display/#` を read/write
- HA のユーザーは `home/raspi3/display/#` を read/write
- 不要な他トピックへのアクセスは許可しない

同一 LAN 内だけで使う場合でも、broker の認証を有効にします。LAN 外から接続する場合は TLS を必須にします。

## 制御元は Home Assistant に一本化

Pi 側には夜間 cron、systemd timer、`@reboot` の時刻制御を置きません。Pi の役割は「MQTT を受信して画面を操作し、結果を返す」ことだけです。

0:00〜8:00 の夜間制御を行う場合は、HA の automation から MQTT switch を操作します。

```yaml
automation:
  - id: raspi3_display_off_at_night
    alias: "ラズパイ画面を夜間OFF"
    triggers:
      - trigger: time
        at: "00:00:00"
    actions:
      - action: switch.turn_off
        target:
          entity_id: switch.raspi3_hdmi_display
    mode: single

  - id: raspi3_display_on_in_morning
    alias: "ラズパイ画面を朝ON"
    triggers:
      - trigger: time
        at: "08:00:00"
    actions:
      - action: switch.turn_on
        target:
          entity_id: switch.raspi3_hdmi_display
    mode: single
```

`switch.raspi3_hdmi_display` は、実際に HA が作成した entity_id に置き換えます。HA のタイムゾーンを `Asia/Tokyo` に設定しておけば、夜間の判定も HA の日本時間で行われます。

Pi が再起動して `online` に戻ったときは、HA 側の現在のスイッチ状態を command topic に再送する automation を追加します。command topic は retain しない方針を維持しつつ、Pi 再起動後の状態を HA の意図へ戻せます。

```yaml
automation:
  - id: raspi3_display_resend_after_online
    alias: "ラズパイ画面の状態を再送"
    triggers:
      - trigger: mqtt
        topic: "home/raspi3/display/availability"
        payload: "online"
    conditions:
      - condition: template
        value_template: >-
          {{ states('switch.raspi3_hdmi_display') in ['on', 'off'] }}
    actions:
      - action: mqtt.publish
        data:
          topic: "home/raspi3/display/hdmi/set"
          payload: >-
            {{ 'ON' if is_state('switch.raspi3_hdmi_display', 'on') else 'OFF' }}
          qos: 1
          retain: false
    mode: restart
```

この automation は、`online` を受けたときだけ現在の HA 状態を一度再送します。Pi 側が retained command を直接実行する仕組みにはしません。

## エラー時の扱い

| 状況 | Pi 側 | HA 側 |
| --- | --- | --- |
| MQTT broker に接続できない | 再接続を続ける | unavailable または最後の状態 |
| Wayland が未起動 | 再試行し、成功まで state を確定しない | 最後の状態を維持 |
| `wlr-randr` が失敗 | state を変更せずログへ記録 | 誤った成功状態を表示しない |
| 不正 payload | 操作せず警告ログ | state を変更しない |
| HDMI ケーブル抜け・対象出力なし | `UNKNOWN` として記録 | availability または診断情報で確認 |
| Pi 再起動 | user service 起動後に `online` を通知 | HA が現在のスイッチ状態を再送 |

状態を無理に OFF と断定すると、実際はケーブル抜けなのに HA が正常と表示するため、`UNKNOWN` を扱える内部状態を持たせます。HA の MQTT switch は ON/OFF を基本表示とし、詳細な原因はログまたは error topic で確認します。

## 実装順序

1. 共通ヘルパーを作成し、`on` / `off` / `status` を Pi 上で単体確認する。
2. MQTT ブリッジを作成し、ローカルの test topic で ON/OFF、再接続、不正 payload を確認する。
3. systemd user service として登録し、ログイン直後・Wayland 起動遅延・Pi 再起動を確認する。
4. HA の MQTT switch を登録し、HA の操作と HA 再起動後の状態表示を確認する。
5. HA の夜間 automation と online 復帰時の状態再送を追加して確認する。
6. Pi 側の cron / systemd timer が残っていないこと、broker 認証、ACL、retain、LWT を確認してから本番 topic を使用する。

## 受け入れ条件

- HA のスイッチを ON にすると、Pi の `HDMI-A-1` が ON になり、HA が ON になる。
- HA のスイッチを OFF にすると、Pi の `HDMI-A-1` が OFF になり、HA が OFF になる。
- Pi の操作結果が state topic に反映され、HA が表示できる。
- HA、Pi、MQTT broker のいずれかを再起動しても、古い command が勝手に再実行されない。
- broker が一時停止しても、ブリッジが自動再接続し、online 状態へ戻る。
- Wayland の起動が遅れても、再試行後に操作が成功する。
- 0:00 の OFF、8:00 の ON、Pi 再起動後の HA 状態再送が維持される。
- Pi 側にスケジュールがなく、HA の操作だけで状態が決まる。
- MQTT パスワードなどの秘密情報がリポジトリに入らない。

## 手動確認コマンド

MQTT broker のアドレスや認証情報は環境に合わせて置き換えます。command topic は retain なしで発行します。

```sh
mosquitto_pub -h MQTT_BROKER -u MQTT_USER -P MQTT_PASSWORD \
  -t home/raspi3/display/hdmi/set -m ON -q 1

mosquitto_pub -h MQTT_BROKER -u MQTT_USER -P MQTT_PASSWORD \
  -t home/raspi3/display/hdmi/set -m OFF -q 1
```

Pi 上で Wayland の状態を確認する場合は、次の環境を設定してから実行します。

```sh
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-0
wlr-randr
```

## 設計上の注意

- `xrandr` ではなく Wayland 用の `wlr-randr` を使用する。
- `HDMI-A-1` は現在の実機で確認した出力名にすぎず、別の Pi や別の compositor では変わり得る。`DISPLAY_OUTPUT` で明示指定するか、物理出力が1つだけなら `auto` を使う。
- MQTT ブリッジを systemd user service で動かす場合でも、Wayland 環境を明示する。
- `set` topic を retain すると、再接続時に古い命令が実行されるため retain しない。再起動後の復旧は HA が現在の状態を再送して行う。
- state topic を retain しても、それは最後に成功した状態であり、画面の物理的な発光状態を保証するものではない。
- systemd のサービスを root で動かさず、Wayland セッションを所有する `pi` ユーザーで動かす。
- HA 以外の時刻スケジュールを Pi に追加しない。将来の自動化も HA 側へ集約する。

## 現在の状態

このリポジトリには、設計に沿った Pi 側の実装と HA 設定例を含めています。

```text
bin/display-control                  # Wayland出力の共通ヘルパー
mqtt/display-mqtt-bridge.py          # MQTTブリッジ
systemd/raspi-display-mqtt.service   # systemd user service
config/mqtt.env.example               # 秘密情報を含まない設定雛形
ha/mqtt-switch.yaml                   # HA MQTT switch の設定例
ha/automations.yaml                   # 夜間制御・online復帰時再送の例
scripts/install.sh                    # Piへの配置スクリプト
tests/test_bridge.py                  # MQTT境界の単体テスト
```

### Pi への配置

Raspbian 側で必要なパッケージを入れます。MQTT broker が別ホストにある場合は、broker の認証・ACLも先に設定してください。

```sh
sudo apt install python3-paho-mqtt util-linux
sudo ./scripts/install.sh
sudoedit /etc/raspi-ha-display-control/mqtt.env
sudo chown pi:pi /etc/raspi-ha-display-control/mqtt.env
sudo chmod 600 /etc/raspi-ha-display-control/mqtt.env
```

その後、Wayland セッションを所有する `pi` ユーザーでサービスを有効化します。

```sh
systemctl --user daemon-reload
systemctl --user enable --now raspi-display-mqtt.service
systemctl --user status raspi-display-mqtt.service
journalctl --user -u raspi-display-mqtt.service -f
```

`scripts/install.sh` は既存の `mqtt.env` を上書きしません。サービスの起動前に、雛形のbrokerホスト・ユーザー名・パスワードを実際の値へ変更してください。TLSを使う場合は `MQTT_TLS=true` とし、必要に応じて `MQTT_TLS_CA_CERT` を設定します。

### ローカル検証

Pi 実機に接続せず、Pythonの構文・シェルの構文・MQTT境界のテストを実行できます。

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile mqtt/display-mqtt-bridge.py
sh -n bin/display-control scripts/install.sh
sh tests/test_display_control.sh
git diff --check
```

本番移行時には、Pi 実機で `bin/display-control status` と ON/OFFを確認したうえで、既存の夜間用 cron、systemd timer、`hdmi-night-toggle` の時刻制御が残っていないことを確認します。それらの削除は実機上の既存設定を確認してから行い、Pi側には時刻スケジュールを追加しません。
