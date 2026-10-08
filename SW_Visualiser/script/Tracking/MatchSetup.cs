using UnityEngine;
using BridgeRush.Match;

namespace BridgeRush.Tracking
{
    public class MatchSetup : MonoBehaviour
    {
        public enum GameMode { VsCpu, VsPlayer }
        public enum SelectedPlayer { None, Player1, Player2 }

        [SerializeField] private ArenaFrame arenaFrame;
        [SerializeField] private MatchState matchState;
        [SerializeField] private float cpuOpponentDistanceMeters = 3f;
        [SerializeField] private int player1OpponentMarkerId = 1;
        [SerializeField] private int player2OpponentMarkerId = 0;

        public bool HasChosenMode { get; private set; }
        public GameMode Mode { get; private set; }

        public SelectedPlayer Selected { get; private set; } = SelectedPlayer.None;
        public bool HasSelected { get; private set; }

        private void Awake()
        {
            if (arenaFrame == null || matchState == null)
            {
                Debug.LogError($"{nameof(MatchSetup)}: needs an Arena Frame and a Match State.", this);
                enabled = false;
            }
        }

        public void DebugReset()
        {
            HasChosenMode = false;
            HasSelected = false;
            Selected = SelectedPlayer.None;
        }

        public void ChooseVsPlayer()
        {
            Mode = GameMode.VsPlayer;
            HasChosenMode = true;
        }

        public void ChooseVsCpu()
        {
            Mode = GameMode.VsCpu;
            HasChosenMode = true;
        }

        public void ActivateCpuOpponent()
        {
            if (Mode != GameMode.VsCpu) return;
            arenaFrame.SetFakeOpponent(cpuOpponentDistanceMeters);
            matchState.SetEnemyBotEnabled(true);
        }

        public void SelectPlayer1()
        {
            Selected = SelectedPlayer.Player1;
            Select(player1OpponentMarkerId);
        }

        public void SelectPlayer2()
        {
            Selected = SelectedPlayer.Player2;
            Select(player2OpponentMarkerId);
        }

        private void Select(int opponentMarkerId)
        {
            arenaFrame.SetOpponentMarkerId(opponentMarkerId);
            HasSelected = true;
        }
    }
}
