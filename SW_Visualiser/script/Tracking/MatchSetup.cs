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
        private const float cpuOpponentDistanceMeters = 2f;
        [SerializeField] private int player1OpponentMarkerId = 1;
        [SerializeField] private int player2OpponentMarkerId = 0;

        public bool HasChosenMode { get; private set; }
        public GameMode Mode { get; private set; }

        // A tutorial is a VS CPU match (local rules, fake opponent, local countdown) with no enemy bot, free summons and no end.
        // TutorialCoach runs the lesson. Mode stays VsCpu, so everything that treats VS CPU as a local game still does.
        public bool IsTutorial { get; private set; }

        public SelectedPlayer Selected { get; private set; } = SelectedPlayer.None;
        public bool HasSelected { get; private set; }
        public bool IsSetupComplete => HasChosenMode && HasSelected;

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
            LeaveTutorial();
            HasChosenMode = false;
            HasSelected = false;
            Selected = SelectedPlayer.None;
        }

        public void ChooseVsPlayer()
        {
            LeaveTutorial();
            Mode = GameMode.VsPlayer;
            HasChosenMode = true;
        }

        public void ChooseVsCpu()
        {
            LeaveTutorial();
            Mode = GameMode.VsCpu;
            HasChosenMode = true;
        }

        public void ChooseTutorial()
        {
            Mode = GameMode.VsCpu;
            IsTutorial = true;
            HasChosenMode = true;
            // Nothing in a tutorial depends on which player you are, so the player screen is skipped.
            SelectPlayer1();
            ActivateCpuOpponent();
        }

        public void ActivateCpuOpponent()
        {
            if (Mode != GameMode.VsCpu) return;
            arenaFrame.SetFakeOpponent(cpuOpponentDistanceMeters);
            matchState.SetEnemyBotEnabled(!IsTutorial);
            if (IsTutorial) matchState.GodMode = true;
        }

        // The lesson is over: the same player goes on to a normal VS CPU match. The caller has already reset the match and
        // forgotten the old fake opponent; this clears the tutorial's flags (free summons, endless clock, no bot) and puts
        // the CPU opponent back, with its bot.
        public void FinishTutorialIntoVsCpu()
        {
            if (!IsTutorial) return;
            LeaveTutorial();
            Mode = GameMode.VsCpu;
            HasChosenMode = true;
            ActivateCpuOpponent();
        }

        // Back out of a tutorial: its free summons, endless clock and missing bot must not leak into the next mode.
        private void LeaveTutorial()
        {
            if (!IsTutorial) return;
            IsTutorial = false;
            matchState.GodMode = false;
            matchState.EndlessMatch = false;
            matchState.SetEnemyBotEnabled(false);
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
