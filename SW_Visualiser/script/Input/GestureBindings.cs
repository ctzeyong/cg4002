using BridgeRush.Gameplay;
using BridgeRush.Match;

namespace BridgeRush.Input
{
    /// <summary>
    /// The gestures the game knows by name. The comms hook (AiGestureHook) parses the name in the classifier's event onto
    /// this enum, so a gesture the glove can send must be listed here, and one that is not listed is rejected as unknown.
    /// </summary>
    public enum GestureSymbol
    {
        Warrior,
        Rogue,
        Mage,
        Tank,
        Hero,
        Lightning,
        Fireball,
        Shield
    }

    /// <summary>What a recognised gesture does in the game, independent of where it came from.</summary>
    public enum GameAction
    {
        None,
        SpawnWarrior,
        SpawnRogue,
        SpawnMage,
        SpawnTank,
        SpawnHero,
        CastLightning,
        CastFireball
    }

    /// <summary>
    /// The single place that says which gesture means which action, for the glove (via the comms pipeline) and for the
    /// debug menu. Changing a binding here changes it everywhere. The gesture names must be the ones the classifier sends.
    /// </summary>
    public static class GestureBindings
    {
        /// <summary>
        /// Which gesture means which action. This is the one table to edit:
        /// The glove sends the name of the unit or spell it recognised: warrior, rogue, mage, tank (or shield), hero,
        /// lightning, fireball.
        /// </summary>
        public static GameAction FromSymbol(GestureSymbol symbol) => symbol switch
        {
            GestureSymbol.Warrior => GameAction.SpawnWarrior,
            GestureSymbol.Rogue => GameAction.SpawnRogue,
            GestureSymbol.Mage => GameAction.SpawnMage,
            GestureSymbol.Tank => GameAction.SpawnTank,
            GestureSymbol.Shield => GameAction.SpawnTank,
            GestureSymbol.Hero => GameAction.SpawnHero,
            GestureSymbol.Lightning => GameAction.CastLightning,
            GestureSymbol.Fireball => GameAction.CastFireball,
            _ => GameAction.None
        };

        /// <summary>The same table by the gesture's name (a string), e.g. from the debug menu. Names are not case sensitive.</summary>
        public static GameAction FromGlove(string gesture)
        {
            string name = (gesture ?? "").Trim();
            foreach (char c in name)
            {
                if (!char.IsLetter(c)) return GameAction.None;
            }
            return System.Enum.TryParse(name, true, out GestureSymbol symbol) && System.Enum.IsDefined(typeof(GestureSymbol), symbol)
                ? FromSymbol(symbol)
                : GameAction.None;
        }

        public static readonly string[] GloveGestures = { "warrior", "rogue", "mage", "tank", "hero", "lightning", "fireball" };

        /// <summary>The glove gesture bound to an action, or null. No longer used for the card icons, which show <c>CardMotions</c> (the planned motions) until the glove is retrained.</summary>
        public static string GloveGestureFor(GameAction action)
        {
            foreach (var gesture in GloveGestures)
                if (FromGlove(gesture) == action) return gesture;
            return null;
        }

        public static string ActionName(GameAction action) => action switch
        {
            GameAction.SpawnWarrior => "Warrior",
            GameAction.SpawnRogue => "Rogue",
            GameAction.SpawnMage => "Mage",
            GameAction.SpawnTank => "Tank",
            GameAction.SpawnHero => "Hero",
            GameAction.CastLightning => "Lightning Strike",
            GameAction.CastFireball => "Fireball",
            _ => "nothing"
        };

        /// <summary>Why <see cref="Perform"/> returned false, in words for the player.</summary>
        public static string DescribeFailure(GameAction action, MatchState matchState, SpellCaster spellCaster)
        {
            if (matchState == null) return "The match isn't ready.";

            return action switch
            {
                GameAction.SpawnWarrior => matchState.DescribeSpawnFailure(UnitType.Warrior),
                GameAction.SpawnRogue => matchState.DescribeSpawnFailure(UnitType.Rogue),
                GameAction.SpawnMage => matchState.DescribeSpawnFailure(UnitType.Mage),
                GameAction.SpawnTank => matchState.DescribeSpawnFailure(UnitType.Tank),
                GameAction.SpawnHero => matchState.DescribeSpawnFailure(UnitType.Hero),
                GameAction.CastLightning => spellCaster != null ? spellCaster.DescribeCastFailure("lightning") : "No spell caster.",
                GameAction.CastFireball => spellCaster != null ? spellCaster.DescribeCastFailure("fireball") : "No spell caster.",
                _ => "No action is bound to that gesture."
            };
        }

        /// <summary>Run the action for the local player. False if it was not possible (no mana, no valid aim, match not started).</summary>
        public static bool Perform(GameAction action, MatchState matchState, SpellCaster spellCaster)
        {
            if (matchState == null) return false;

            return action switch
            {
                GameAction.SpawnWarrior => matchState.SpawnUnit(UnitType.Warrior),
                GameAction.SpawnRogue => matchState.SpawnUnit(UnitType.Rogue),
                GameAction.SpawnMage => matchState.SpawnUnit(UnitType.Mage),
                GameAction.SpawnTank => matchState.SpawnUnit(UnitType.Tank),
                GameAction.SpawnHero => matchState.SpawnUnit(UnitType.Hero),
                GameAction.CastLightning => spellCaster != null && spellCaster.CastLightningStrike(),
                GameAction.CastFireball => spellCaster != null && spellCaster.CastFireball(),
                _ => false
            };
        }
    }
}
