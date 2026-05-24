// 2 フレーム後に fn を実行するヘルパ。 messages 反映直後の scroll や layout が
// 1 rAF だと未確定で「下まで来ない」 ことがある対策で、 2 rAF 待ってから走らせる。
// 旧コードでは requestAnimationFrame の二重ネストを App.jsx で個別に書いていて、
// ここに集約した。
export function nextNextFrame(fn) {
  requestAnimationFrame(() => requestAnimationFrame(fn))
}
