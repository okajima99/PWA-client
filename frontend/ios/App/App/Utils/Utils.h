//
//  Utils.h
//  App (claude-pwa-client iOS app)
//
//  公式 moonlight-ios の Limelight/Utility/Utils.h から、 Connection.m が参照する
//  関数 (addressPortStringToAddress / isActiveNetworkVPN) のみ抜粋した最小版。
//  原典: https://github.com/moonlight-stream/moonlight-ios (GPL-3.0)
//

#import <Foundation/Foundation.h>

@interface Utils : NSObject

+ (BOOL) isActiveNetworkVPN;
+ (NSString*) addressPortStringToAddress:(NSString*)addressPort;
+ (BOOL) parseAddressPortString:(NSString*)addressPort
                         address:(NSRange*)address
                            port:(NSRange*)port;

@end
