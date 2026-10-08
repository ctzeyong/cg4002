using System.Collections.Generic;
using UnityEngine;
using BridgeRush.Match;
using BridgeRush.Gameplay;
using BridgeRush.UI;

namespace BridgeRush.Input
{
    public class GesturePad : MonoBehaviour
    {
        [SerializeField] private MonoBehaviour recognizerBehaviour;
        [SerializeField] private MatchState matchState;
        [SerializeField] private SpellCaster spellCaster;
        [SerializeField] private Canvas canvas;
        [SerializeField] private float minSegmentDistance = 4f;
        [SerializeField] private float strokeThickness = 4f;
        [SerializeField] private Color strokeColor = Color.cyan;
        [SerializeField] private Color acceptedColor = Color.green;
        [SerializeField] private Color rejectedColor = Color.red;
        [SerializeField] private float feedbackDisplaySeconds = 1.5f;

        private IGestureRecognizer recognizer;
        private readonly List<Vector2> currentStroke = new();
        private bool isDrawing;
        private Texture2D lineTexture;
        private string feedbackMessage;
        private Color feedbackColor;
        private float feedbackTimeRemaining;
        private float PanelSize => HudLayout.GesturePadSize * canvas.scaleFactor;
        private Rect PanelRect
        {
            get
            {
                float scaleFactor = canvas.scaleFactor;
                float dockHeightPixels = (HudLayout.HandTotalHeight + 16f) * scaleFactor;
                float gapAboveHandPixels = 24f * scaleFactor;
                float top = Screen.height - dockHeightPixels - gapAboveHandPixels - PanelSize;
                return new Rect(Screen.width / 2f - PanelSize / 2f, top, PanelSize, PanelSize);
            }
        }
        private void Awake()
        {
            recognizer = recognizerBehaviour as IGestureRecognizer;
            if (recognizer == null || matchState == null || spellCaster == null || canvas == null)
            {
                Debug.LogError($"{nameof(GesturePad)}: needs a Canvas, Match State, Spell Caster and a behaviour implementing {nameof(IGestureRecognizer)}.", this);
                enabled = false;
                return;
            }

            lineTexture = new Texture2D(1, 1);
            lineTexture.SetPixel(0, 0, Color.white);
            lineTexture.Apply();
        }
        private void Update()
        {
            if (feedbackTimeRemaining > 0f)
            {
                feedbackTimeRemaining -= Time.deltaTime;
                if (feedbackTimeRemaining <= 0f) feedbackMessage = null;
            }
        }
        private void OnGUI()
        {
            var rect = PanelRect;
            GUI.Box(rect, GUIContent.none);
            GUI.Label(new Rect(rect.x - 60f, rect.y - 34f, rect.width + 120f, 28f),
                string.IsNullOrEmpty(feedbackMessage) ? "Draw here" : feedbackMessage,
                new GUIStyle(GUI.skin.label) { alignment = TextAnchor.MiddleCenter, fontSize = 18, normal = { textColor = string.IsNullOrEmpty(feedbackMessage) ? Color.white : feedbackColor } });

            HandleInput(rect);
            DrawStroke();
        }
        private void HandleInput(Rect rect)
        {
            var e = Event.current;
            if (e == null) return;
            switch (e.type)
            {
                case EventType.MouseDown:
                    if (!rect.Contains(e.mousePosition)) return;
                    isDrawing = true;
                    currentStroke.Clear();
                    currentStroke.Add(e.mousePosition);
                    e.Use();
                    break;
                case EventType.MouseDrag:
                    if (!isDrawing) return;
                    if (currentStroke.Count == 0 || Vector2.Distance(currentStroke[^1], e.mousePosition) >= minSegmentDistance)
                    {
                        currentStroke.Add(e.mousePosition);
                    }
                    e.Use();
                    break;
                case EventType.MouseUp:
                    if (!isDrawing) return;
                    isDrawing = false;
                    currentStroke.Add(e.mousePosition);
                    OnStrokeCompleted();
                    e.Use();
                    break;
            }
        }
        private void DrawStroke()
        {
            for (int i = 1; i < currentStroke.Count; i++)
            {
                DrawGUILine(currentStroke[i - 1], currentStroke[i], strokeThickness, strokeColor);
            }
        }
        private void DrawGUILine(Vector2 a, Vector2 b, float thickness, Color color)
        {
            Vector2 delta = b - a;
            float length = delta.magnitude;
            if (length < 0.001f) return;
            float angle = Mathf.Atan2(delta.y, delta.x) * Mathf.Rad2Deg;
            var previousMatrix = GUI.matrix;
            var previousColor = GUI.color;
            GUI.color = color;
            GUIUtility.RotateAroundPivot(angle, a);
            GUI.DrawTexture(new Rect(a.x, a.y - thickness / 2f, length, thickness), lineTexture);
            GUI.matrix = previousMatrix;
            GUI.color = previousColor;
        }

        private void OnStrokeCompleted()
        {
            if (!recognizer.TryRecognize(currentStroke, out var symbol, out var confidence))
            {
                ShowFeedback("Rejected - not recognized, redraw", rejectedColor);
                currentStroke.Clear();
                return;
            }
            if (!TryPerformAction(symbol))
            {
                ShowFeedback($"{symbol} recognized ({confidence:P0}) - not enough mana", rejectedColor);
                currentStroke.Clear();
                return;
            }
            ShowFeedback($"{symbol} accepted ({confidence:P0})", acceptedColor);
            currentStroke.Clear();
        }
        private bool TryPerformAction(GestureSymbol symbol) => symbol switch
        {
            GestureSymbol.Circle => matchState.SpawnUnit(UnitType.Warrior),
            GestureSymbol.Caret => matchState.SpawnUnit(UnitType.Rogue),
            GestureSymbol.Arc => matchState.SpawnUnit(UnitType.Mage),
            GestureSymbol.Triangle => matchState.SpawnUnit(UnitType.Tank),
            GestureSymbol.Star => matchState.SpawnUnit(UnitType.Hero),
            GestureSymbol.Z => spellCaster.CastLightningStrike(),
            GestureSymbol.Infinity => spellCaster.CastOvercharge(),
            _ => false
        };
        private void ShowFeedback(string message, Color color)
        {
            feedbackMessage = message;
            feedbackColor = color;
            feedbackTimeRemaining = feedbackDisplaySeconds;
        }
    }
}
