// streamView (= MoonlightPlugin が parent.addSubview する Mac 画面描画用 UIView) の
// subclass。 view.frame が動的に setVideoFrame で書き換わるたびに、 内部の
// AVSampleBufferDisplayLayer (= VideoDecoderRenderer が addSublayer した sublayer) を
// bounds に追従させる。 さもないと IDR frame 受信時 (= reinitializeDisplayLayer) の
// view.bounds で displayLayer.frame が固定され、 setVideoFrame で view.frame を縮めても
// displayLayer は初期の画面全体サイズのまま残って status bar を侵食する症状が出る。

import UIKit

class StreamHostView: UIView {
    override init(frame: CGRect) {
        super.init(frame: frame)
        // AVSampleBufferDisplayLayer は VideoToolbox 経路で parent view の枠を超えて
        // 描画するケースがある (= layer の bounds を計算上は守るが、 hardware compositor
        // が clip を効かせない)。 clipsToBounds = true で view 枠を越えた描画を遮断する。
        self.clipsToBounds = true
    }
    required init?(coder: NSCoder) {
        super.init(coder: coder)
        self.clipsToBounds = true
    }
    override func layoutSubviews() {
        super.layoutSubviews()
        let count = self.layer.sublayers?.count ?? 0
        HavenDebugLog("StreamHostView::layoutSubviews" as NSString,
                      "bounds=\(self.bounds) frame=\(self.frame) sublayers=\(count)" as NSString)
        for layer in self.layer.sublayers ?? [] {
            layer.frame = self.bounds
        }
    }
}
