#import <UIKit/UIKit.h>

// style: 0 = light, 1 = medium, 2 = heavy. A single short tap from the Taptic Engine (iPhone 7 and later).
extern "C" void BridgeRushHaptic(int style)
{
    if (@available(iOS 10.0, *))
    {
        UIImpactFeedbackStyle feedbackStyle = style <= 0 ? UIImpactFeedbackStyleLight
            : (style == 1 ? UIImpactFeedbackStyleMedium : UIImpactFeedbackStyleHeavy);
        UIImpactFeedbackGenerator *generator = [[UIImpactFeedbackGenerator alloc] initWithStyle:feedbackStyle];
        [generator prepare];
        [generator impactOccurred];
    }
}
