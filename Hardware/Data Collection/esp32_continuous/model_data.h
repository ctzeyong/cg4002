#pragma once
// Safe placeholder: run collection, train, validate and export before detection.
constexpr bool MODEL_READY = false;
constexpr int MODEL_RAW_HZ = 500;
constexpr char MODEL_ID[] = "UNTRAINED-gesture-packet-v4";
struct TreeNode { int feature, left, right; float threshold, score; };
static const TreeNode TREE_0[] = {{-1,-1,-1,0.f,0.f}};
constexpr int TREE_COUNT = 1;
static const TreeNode* const TREES[] = {TREE_0};
constexpr float START_THRESHOLD = .4f;
constexpr float STOP_THRESHOLD = .2f;
constexpr long long STOP_HOLD_US = 200000;
