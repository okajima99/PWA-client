//
//  Utils.m
//  App (claude-pwa-client iOS app)
//
//  公式 moonlight-ios (GPL-3.0) の Limelight/Utility/Utils.m から
//  Connection.m が参照する 3 関数のみを抜粋した最小実装。
//

#import "Utils.h"
#import <SystemConfiguration/SystemConfiguration.h>
#import <CFNetwork/CFNetwork.h>

@implementation Utils

+ (BOOL)isActiveNetworkVPN {
    NSDictionary *dict = CFBridgingRelease(CFNetworkCopySystemProxySettings());
    NSArray *keys = [dict[@"__SCOPED__"] allKeys];
    for (NSString *key in keys) {
        if ([key containsString:@"tap"] ||
            [key containsString:@"tun"] ||
            [key containsString:@"ppp"] ||
            [key containsString:@"ipsec"]) {
            return YES;
        }
    }
    return NO;
}

+ (BOOL) parseAddressPortString:(NSString*)addressPort
                         address:(NSRange*)address
                            port:(NSRange*)port {
    if (![addressPort containsString:@":"]) {
        // ポート区切り / IPv6 区切りが無ければ全体がアドレス
        *address = NSMakeRange(0, [addressPort length]);
        *port = NSMakeRange(NSNotFound, 0);
        return TRUE;
    }

    NSInteger locationOfOpeningBracket = [addressPort rangeOfString:@"["].location;
    NSInteger locationOfClosingBracket = [addressPort rangeOfString:@"]"].location;
    if (locationOfOpeningBracket != NSNotFound || locationOfClosingBracket != NSNotFound) {
        // [...] があれば IPv6 扱い
        if (locationOfOpeningBracket == NSNotFound || locationOfClosingBracket == NSNotFound ||
            locationOfClosingBracket < locationOfOpeningBracket) {
            return FALSE;
        }
        *address = NSMakeRange(locationOfOpeningBracket + 1,
                               locationOfClosingBracket - locationOfOpeningBracket - 1);
    } else {
        // IPv4 はコロン区切り
        *address = NSMakeRange(0, [addressPort rangeOfString:@":"].location);
    }

    NSUInteger remainingStringLocation = address->location + address->length;
    NSRange remainingStringRange = NSMakeRange(remainingStringLocation,
                                               [addressPort length] - remainingStringLocation);
    NSInteger locationOfPortSeparator = [addressPort rangeOfString:@":"
                                                            options:0
                                                              range:remainingStringRange].location;
    if (locationOfPortSeparator != NSNotFound) {
        *port = NSMakeRange(locationOfPortSeparator + 1,
                            [addressPort length] - locationOfPortSeparator - 1);
    } else {
        *port = NSMakeRange(NSNotFound, 0);
    }
    return TRUE;
}

+ (NSString*) addressPortStringToAddress:(NSString*)addressPort {
    NSRange addressRange, portRange;
    if (![self parseAddressPortString:addressPort address:&addressRange port:&portRange]) {
        return nil;
    }
    return [addressPort substringWithRange:addressRange];
}

@end
