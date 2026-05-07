import UIKit
import Capacitor
import AVFoundation

// 画面回転 lock 管理 (= web から MoonlightPlugin.setOrientationLock で切替)
@objc class HavenOrientation: NSObject {
    @objc static var locked: String = "auto"  // "auto" / "portrait" / "landscape" / "landscapeLeft" / "landscapeRight"
}

@UIApplicationMain
class AppDelegate: UIResponder, UIApplicationDelegate {

    var window: UIWindow?

    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        // build 29 から維持: SDL2 は main を SDL_main にハイジャックする仕組みだが
        // SDL_MAIN_HANDLED 定義済なので明示で SDL_SetMainReady() を呼ぶ。 これがないと
        // SDL_InitSubSystem が「not initialized」 で失敗する。
        SDL_SetMainReady()

        // build 33: AVAudioSession の設定は AppDelegate ではなく Connection.m::ArInit で行う
        // (= 公式 moonlight-ios と一致、 SDL_InitSubSystem 直前で setCategory するパターン)。
        // AppDelegate で先に取ると WKWebView 起動で奪われ + WKWebView がデバイスを Ambient 系で
        // 開いた状態に固定されて、 SDL の preferred sample rate が無視される (SDL #9635) → 機械音。
        // ArInit に集約することで SDL audio device 開く直前のタイミング保証する。
        return true
    }

    func applicationWillResignActive(_ application: UIApplication) {
        // Sent when the application is about to move from active to inactive state. This can occur for certain types of temporary interruptions (such as an incoming phone call or SMS message) or when the user quits the application and it begins the transition to the background state.
        // Use this method to pause ongoing tasks, disable timers, and invalidate graphics rendering callbacks. Games should use this method to pause the game.
    }

    func applicationDidEnterBackground(_ application: UIApplication) {
        // Use this method to release shared resources, save user data, invalidate timers, and store enough application state information to restore your application to its current state in case it is terminated later.
        // If your application supports background execution, this method is called instead of applicationWillTerminate: when the user quits.
    }

    func applicationWillEnterForeground(_ application: UIApplication) {
        // Called as part of the transition from the background to the active state; here you can undo many of the changes made on entering the background.
    }

    func applicationDidBecomeActive(_ application: UIApplication) {
        // Restart any tasks that were paused (or not yet started) while the application was inactive. If the application was previously in the background, optionally refresh the user interface.
    }

    func applicationWillTerminate(_ application: UIApplication) {
        // Called when the application is about to terminate. Save data if appropriate. See also applicationDidEnterBackground:.
    }

    func application(_ app: UIApplication, open url: URL, options: [UIApplication.OpenURLOptionsKey: Any] = [:]) -> Bool {
        // Called when the app was launched with a url. Feel free to add additional processing here,
        // but if you want the App API to support tracking app url opens, make sure to keep this call
        return ApplicationDelegateProxy.shared.application(app, open: url, options: options)
    }

    func application(_ application: UIApplication, continue userActivity: NSUserActivity, restorationHandler: @escaping ([UIUserActivityRestoring]?) -> Void) -> Bool {
        // Called when the app was launched with an activity, including Universal Links.
        // Feel free to add additional processing here, but if you want the App API to support
        // tracking app url opens, make sure to keep this call
        return ApplicationDelegateProxy.shared.application(application, continue: userActivity, restorationHandler: restorationHandler)
    }

    // 画面回転 lock 制御 (HavenOrientation.locked を読んで supported orientation を返す)
    func application(_ application: UIApplication, supportedInterfaceOrientationsFor window: UIWindow?) -> UIInterfaceOrientationMask {
        switch HavenOrientation.locked {
        case "portrait": return .portrait
        case "landscape": return .landscape
        case "landscapeLeft": return [.landscapeLeft]
        case "landscapeRight": return [.landscapeRight]
        default: return .all
        }
    }

}
