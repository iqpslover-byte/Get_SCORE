# Get_SCORE

OP's LAB Maps の「答え合わせ」台帳。打上げ前の軌道予測（航行警報からの傾斜角推定＋射場/時刻からのRAAN計算）と、打上げ後の実測TLEを毎日自動で突き合わせ、予測誤差を累積記録する。

## 仕組み（毎日 03:15 UTC）

1. **予測** … Get_LAUNCHES の予定打上げ × Get_NAVWARN の警報を突合（anchor 200km＋日付±3日）。
   傾斜角は H方式（各ゾーン重心方位→コリドー折返し→最遠50%集約）、RAANは軌道法線方式
   （いずれも OPs_LAB_Maps.html と同一の計算・補正0）。
2. **凍結** … リフトオフ確認（status成功/失敗 or net+6h経過）で予測を書き込み禁止に。
   以後この打上げの pred は不変（Gitコミット履歴が監査証跡）。
3. **答え合わせ** … satcat.json の COSPAR グループ（打上げ日±1日＋射場コード照合）で物体を同定。
   実測傾斜角＝PAYLOAD中央値。実測RAANは tle_recent.json のTLE（リフトオフ+24h以降のエポック優先）を
   J2歳差率でリフトオフ時刻へ巻き戻し。予測RAANは実リフトオフ時刻で再計算して公正に比較。
4. **成績** … ロケット×射場ごとの平均誤差・ばらつき・MAEを stats.json へ。

## 出力

| ファイル | 内容 |
|---|---|
| `data/ledger_YYYY.json` | 台帳（1打上げ=1レコード・追記専用・区域座標の写し込み） |
| `data/stats.json` | 通算成績（ロケット×射場別の d_incl / d_raan 統計） |

## フラグ

`no-navwarn`（警報なし＝採点対象外）/ `launch-failure` / `multi-orbit`（傾斜角ばらつき>5°）/
`ambiguous-cospar`（同定曖昧）/ `site-unmapped`（射場コード未登録・日付のみ同定）/
`early-tle`（+24h未満のTLEで暫定）/ `no-tle-timeout`（45日待ってTLEなし＝軍事等）

## 関連

- 表示側: OP's LAB Maps（打上げ情報モーダルの「答え合わせ」ビュー・実装予定）
- データ元: [Get_LAUNCHES](https://github.com/iqpslover-byte/Get_LAUNCHES) /
  [Get_NAVWARN](https://github.com/iqpslover-byte/Get_NAVWARN) /
  [Get_TLE](https://github.com/iqpslover-byte/Get_TLE)
