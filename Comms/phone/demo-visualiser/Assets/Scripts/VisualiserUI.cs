using UnityEngine;

namespace Comms.Visualiser
{
    /// <summary>
    /// The scrolling message log, plus a field for the broker address.
    ///
    /// IMGUI on purpose: it needs no Canvas, no prefab, no font asset and no EventSystem,
    /// so the whole visualiser is five scripts and a DLL with nothing to wire up in the
    /// Editor. It also sidesteps the project's new Input System package, which would
    /// otherwise need configuring before uGUI buttons responded to touch.
    /// </summary>
    [RequireComponent(typeof(MqttVisualiser))]
    public class VisualiserUI : MonoBehaviour
    {
        // Lay out in a fixed-width virtual space and scale to the screen, so the same
        // layout is legible in the Editor and on a phone.
        private const float ReferenceWidth = 800f;

        private MqttVisualiser _visualiser;
        private string _host;
        private Vector2 _scroll;
        private int _renderedVersion;

        private void Awake() => _visualiser = GetComponent<MqttVisualiser>();

        private void OnGUI()
        {
            // Deferred to the first frame: MqttVisualiser.Start resolves the saved host,
            // and component start order within one GameObject is not defined.
            _host ??= _visualiser.Host ?? MqttConfig.DefaultHost;

            var scale = Screen.width / ReferenceWidth;
            GUI.matrix = Matrix4x4.Scale(new Vector3(scale, scale, 1f));

            GUILayout.BeginArea(new Rect(10, 10, ReferenceWidth - 20, Screen.height / scale - 20));

            GUILayout.BeginHorizontal();
            GUILayout.Label("Broker", GUILayout.Width(60));
            _host = GUILayout.TextField(_host, GUILayout.Width(220));
            if (GUILayout.Button("Connect", GUILayout.Width(100))) Reconnect();
            GUILayout.EndHorizontal();

            GUILayout.Label(_visualiser.Status);
            GUILayout.Label(
                $"received={_visualiser.Received} dup={_visualiser.Duplicates} " +
                $"bad={_visualiser.BadEnvelope} sessions={_visualiser.Sessions} " +
                $"reconnects={_visualiser.Reconnects}");

            // Stick to the bottom as events arrive, the way a log viewer does.
            if (_visualiser.LogVersion != _renderedVersion)
            {
                _renderedVersion = _visualiser.LogVersion;
                _scroll.y = float.MaxValue;
            }

            _scroll = GUILayout.BeginScrollView(_scroll, GUI.skin.box);
            foreach (var line in _visualiser.Log) GUILayout.Label(line);
            GUILayout.EndScrollView();

            GUILayout.EndArea();
        }

        private void Reconnect()
        {
            _visualiser.Host = _host.Trim();
            MqttConfig.SaveHost(_visualiser.Host);
            _visualiser.AddLine($"connecting to {_visualiser.Host}");
            _visualiser.Connect();
        }
    }
}
