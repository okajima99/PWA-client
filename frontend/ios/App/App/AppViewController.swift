// AppViewController.swift
//
// CAPBridgeViewController を subclass して、 Capacitor 6+ で壊れてる
// local custom plugin の自動 registration を手動で行う。
// Capacitor の auto-discovery は npm 経由 plugin にしか効かないので、
// 自前 plugin (MoonlightPlugin) は capacitorDidLoad() で明示登録する必要がある。
//
// 詳細: https://github.com/ionic-team/capacitor/issues/7409
//        https://github.com/ionic-team/capacitor/issues/7443

import Capacitor

class AppViewController: CAPBridgeViewController {
    override func capacitorDidLoad() {
        bridge?.registerPluginInstance(MoonlightPlugin())
    }
}
