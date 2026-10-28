"""Rules and light computer opponents for the board games Laden hosts: chess, othello, checkers and go.

Every game uses the same interface, so the bot can store a game as a plain list of move strings and
replay it. Colours are "white" and "black"; boards are sent to the Mini App as one character per square.
The opponents are plain Python with a time budget per move, so they stay quick on a tiny server."""

import random
import re
import time

import chess

OTHER = {"white": "black", "black": "white"}


class OutOfTime(Exception):
    pass


class Rules:
    kind = title = emoji = ""
    first = "black"  # who moves first
    levels = 5  # levels of the computer opponent
    clock_minutes = 5  # each player's time; the usual online setting for the game
    level_note = "1 = beginner · 3 = casual · 5 = strongest"
    opponent_name = "Laden"

    def new(self):
        raise NotImplementedError

    def replay(self, moves):
        state = self.new()
        for move in moves:
            state = self.play(state, move)
        return state

    def forced(self, state):
        """A move the side to move must make without choosing (e.g. passing in othello), or None."""
        return None

    def label(self, state, move):
        """How a move is written in the notation (before it's played)."""
        return move

    def score(self, state):
        """Extra numbers shown next to the players (e.g. disc counts); None if there aren't any."""
        return None

    def draw_by_timeout(self, state, winner):
        """True if the side that didn't run out of time couldn't have won anyway."""
        return False


# ---------------------------------------------------------------- chess (python-chess; Stockfish lives in bot.py)

CHESS_REASONS = {
    "CHECKMATE": "checkmate", "STALEMATE": "stalemate", "INSUFFICIENT_MATERIAL": "insufficient material",
    "SEVENTYFIVE_MOVES": "the 75-move rule", "FIVEFOLD_REPETITION": "fivefold repetition",
    "FIFTY_MOVES": "the 50-move rule", "THREEFOLD_REPETITION": "threefold repetition",
}


class Chess(Rules):
    kind, title, emoji, first, levels = "chess", "Chess", "♟", "white", 8
    clock_minutes = 10  # rapid
    level_note = "1 = beginner · 3 = casual · 5 = club player · 8 = grandmaster+"
    opponent_name = "Laden (Stockfish)"

    def new(self):
        return chess.Board()

    def replay(self, moves):
        board = chess.Board()
        for move in moves:
            board.push_uci(move)
        return board

    def play(self, board, move):
        board = board.copy()
        try:
            parsed = chess.Move.from_uci(move)
        except ValueError:
            raise ValueError("bad move")
        if parsed not in board.legal_moves:
            raise ValueError("illegal move")
        board.push(parsed)
        return board

    def turn(self, board):
        return "white" if board.turn else "black"

    def legal(self, board):
        return [m.uci() for m in board.legal_moves]

    def outcome(self, board):
        result = board.outcome(claim_draw=True)
        if not result:
            return None
        winner = None if result.winner is None else ("white" if result.winner else "black")
        return winner, CHESS_REASONS.get(result.termination.name, "the rules")

    def snapshot(self, board):
        return board.board_fen()

    def label(self, board, move):
        return board.san(chess.Move.from_uci(move))

    def check_square(self, board):
        return chess.square_name(board.king(board.turn)) if board.is_check() else None

    def draw_by_timeout(self, board, winner):
        return board.has_insufficient_material(chess.WHITE if winner == "white" else chess.BLACK)

    def bot_move(self, board, level):
        """Fallback when Stockfish isn't available: mate if possible, else the juiciest capture or a check."""
        values = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}

        def score(move):
            board.push(move)
            mate, check = board.is_checkmate(), board.is_check()
            board.pop()
            taken = board.piece_type_at(move.to_square) if board.is_capture(move) else None
            return mate, values.get(taken, 0), check, random.random()

        return max(board.legal_moves, key=score).uci()


# ---------------------------------------------------------------- shared search for othello and checkers

def negamax_search(rules, state, depth_limit, seconds):
    """Alpha-beta negamax with iterative deepening; returns the best move found before time runs out."""
    deadline = time.time() + seconds
    moves = rules.ordered_moves(state)
    if len(moves) == 1:
        return moves[0]
    best = random.choice(moves)

    def negamax(node, depth, alpha, beta):
        if time.time() > deadline:
            raise OutOfTime
        if depth == 0 or rules.outcome(node):
            return rules.evaluate(node)
        value = -10 ** 9
        for move in rules.ordered_moves(node):
            value = max(value, -negamax(rules.play(node, move), depth - 1, -beta, -alpha))
            alpha = max(alpha, value)
            if alpha >= beta:
                break
        return value

    for depth in range(1, depth_limit + 1):
        try:
            scored = []
            alpha = -10 ** 9
            for move in moves:
                value = -negamax(rules.play(state, move), depth - 1, -10 ** 9, -alpha)
                scored.append((value + random.random() * 0.01, move))  # tiny noise varies equal moves
                alpha = max(alpha, value)
            scored.sort(reverse=True)
            if depth == 1 or depth % 2 == 0:
                # an odd depth ends on our own move and misses the opponent's reply (e.g. a recapture), so
                # only trust searches that end on theirs
                best = scored[0][1]
            moves = [m for _, m in scored]  # search the best moves first next time
        except OutOfTime:
            break
    return best


# ---------------------------------------------------------------- othello

OTHELLO_DIRECTIONS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
OTHELLO_WEIGHTS = [
    120, -20, 20, 5, 5, 20, -20, 120,
    -20, -40, -5, -5, -5, -5, -40, -20,
    20, -5, 15, 3, 3, 15, -5, 20,
    5, -5, 3, 3, 3, 3, -5, 5,
    5, -5, 3, 3, 3, 3, -5, 5,
    20, -5, 15, 3, 3, 15, -5, 20,
    -20, -40, -5, -5, -5, -5, -40, -20,
    120, -20, 20, 5, 5, 20, -20, 120,
]


class OthelloState:
    __slots__ = ("board", "turn")

    def __init__(self, board, turn):
        self.board, self.turn = board, turn


def othello_square(index):
    return "abcdefgh"[index % 8] + str(index // 8 + 1)  # a1 is top left, as in othello notation


class Othello(Rules):
    kind, title, emoji = "othello", "Othello", "⚫"
    level_note = "1 = beginner · 3 = casual · 5 = strongest"

    def new(self):
        board = ["."] * 64
        board[27], board[28], board[35], board[36] = "w", "b", "b", "w"  # d4 e4 / d5 e5
        return OthelloState(board, "black")

    def flips(self, board, index, disc):
        if board[index] != ".":
            return []
        theirs = "w" if disc == "b" else "b"
        row, col, flipped = index // 8, index % 8, []
        for dr, dc in OTHELLO_DIRECTIONS:
            r, c, line = row + dr, col + dc, []
            while 0 <= r < 8 and 0 <= c < 8 and board[r * 8 + c] == theirs:
                line.append(r * 8 + c)
                r, c = r + dr, c + dc
            if line and 0 <= r < 8 and 0 <= c < 8 and board[r * 8 + c] == disc:
                flipped += line
        return flipped

    def placements(self, board, color):
        disc = color[0]
        return [i for i in range(64) if board[i] == "." and self.flips(board, i, disc)]

    def turn(self, state):
        return state.turn

    def legal(self, state):
        moves = [othello_square(i) for i in self.placements(state.board, state.turn)]
        if moves:
            return moves
        return ["pass"] if self.placements(state.board, OTHER[state.turn]) else []

    def forced(self, state):
        return "pass" if self.legal(state) == ["pass"] else None

    def play(self, state, move):
        if move == "pass":
            if self.legal(state) != ["pass"]:
                raise ValueError("you can only pass when you have no moves")
            return OthelloState(state.board, OTHER[state.turn])
        try:
            index = "abcdefgh".index(move[0]) + (int(move[1:]) - 1) * 8
        except (ValueError, IndexError):
            raise ValueError("bad move")
        if not 0 <= index < 64:
            raise ValueError("bad move")
        disc = state.turn[0]
        flipped = self.flips(state.board, index, disc)
        if not flipped:
            raise ValueError("that square doesn't flip anything")
        board = state.board[:]
        for i in flipped + [index]:
            board[i] = disc
        return OthelloState(board, OTHER[state.turn])

    def outcome(self, state):
        if self.legal(state):
            return None
        black, white = state.board.count("b"), state.board.count("w")
        if black == white:
            return None, f"a {black}–{white} tie"
        return ("black" if black > white else "white"), f"{max(black, white)}–{min(black, white)} discs"

    def snapshot(self, state):
        return "".join(state.board)

    def score(self, state):
        return {"black": state.board.count("b"), "white": state.board.count("w")}

    # computer opponent
    def ordered_moves(self, state):
        moves = self.legal(state)
        return sorted(moves, key=lambda m: -OTHELLO_WEIGHTS[self.index(m)] if m != "pass" else 0)

    @staticmethod
    def index(move):
        return "abcdefgh".index(move[0]) + (int(move[1:]) - 1) * 8

    def evaluate(self, state):
        """Score from the point of view of the side to move."""
        mine, theirs = state.turn[0], OTHER[state.turn][0]
        result = self.outcome(state)
        if result:
            diff = state.board.count(mine) - state.board.count(theirs)
            return 10000 * (1 if diff > 0 else -1 if diff < 0 else 0) + diff
        position = sum(w for w, d in zip(OTHELLO_WEIGHTS, state.board) if d == mine) - \
            sum(w for w, d in zip(OTHELLO_WEIGHTS, state.board) if d == theirs)
        mobility = len(self.placements(state.board, state.turn)) - len(self.placements(state.board, OTHER[state.turn]))
        return position + 8 * mobility

    def bot_move(self, state, level):
        moves = self.legal(state)
        if len(moves) == 1:
            return moves[0]
        if level == 1:
            return random.choice(moves)
        if level == 2:  # greedy: most discs flipped, with some randomness
            return max(moves, key=lambda m: len(self.flips(state.board, self.index(m), state.turn[0])) + random.random() * 3)
        depth, seconds = {3: (2, 0.5), 4: (4, 1.0), 5: (6, 2.0)}[level]
        return negamax_search(self, state, depth, seconds)


# ---------------------------------------------------------------- checkers (English draughts, 8x8)

def checkers_coords(square):
    """Square number 1-32 -> (row, col); row 0 is the top (Black's side)."""
    row, i = divmod(square - 1, 4)
    return row, 2 * i + 1 if row % 2 == 0 else 2 * i


CHECKERS_SQUARE = {checkers_coords(n): n for n in range(1, 33)}


def checkers_neighbours():
    steps, jumps = {}, {}
    for n in range(1, 33):
        row, col = checkers_coords(n)
        for dr in (-1, 1):
            for dc in (-1, 1):
                steps[n, dr, dc] = CHECKERS_SQUARE.get((row + dr, col + dc))
                over, land = CHECKERS_SQUARE.get((row + dr, col + dc)), CHECKERS_SQUARE.get((row + 2 * dr, col + 2 * dc))
                jumps[n, dr, dc] = (over, land) if over and land else None
    return steps, jumps


CHECKERS_STEPS, CHECKERS_JUMPS = checkers_neighbours()
CHECKERS_QUIET_LIMIT = 80  # 40 moves each without a capture or a man moving: draw


class CheckersState:
    __slots__ = ("board", "turn", "quiet")

    def __init__(self, board, turn, quiet):
        self.board, self.turn, self.quiet = board, turn, quiet  # board: dict square -> "b" "w" "B" "W"


class Checkers(Rules):
    kind, title, emoji = "checkers", "Checkers", "⛀"
    level_note = "1 = beginner · 3 = casual · 5 = strongest"

    def new(self):
        board = {n: "b" for n in range(1, 13)}
        board.update({n: "w" for n in range(21, 33)})
        return CheckersState(board, "black", 0)

    @staticmethod
    def directions(piece):
        if piece.isupper():
            return [(-1, -1), (-1, 1), (1, -1), (1, 1)]
        return [(1, -1), (1, 1)] if piece == "b" else [(-1, -1), (-1, 1)]

    @staticmethod
    def promotes(piece, square):
        return (piece == "b" and square >= 29) or (piece == "w" and square <= 4)

    def jump_paths(self, board, square, piece, taken):
        """Every complete jump sequence from this square (a man that is crowned stops there)."""
        paths = []
        for dr, dc in self.directions(piece):
            hop = CHECKERS_JUMPS[square, dr, dc]
            if not hop:
                continue
            over, land = hop
            target = board.get(over)
            if not target or target.lower() == piece.lower() or over in taken or (land in board and land != taken.get("start")):
                continue
            if self.promotes(piece, land):
                paths.append([land])
                continue
            rest = self.jump_paths(board, land, piece, {**taken, over: True})
            paths += [[land] + p for p in rest] if rest else [[land]]
        return paths

    def moves_for(self, state):
        mine = state.turn[0]
        jumps, steps = [], []
        for square, piece in state.board.items():
            if piece.lower() != mine:
                continue
            for path in self.jump_paths(state.board, square, piece, {"start": square}):
                jumps.append("x".join(map(str, [square] + path)))
            if not jumps:
                for dr, dc in self.directions(piece):
                    to = CHECKERS_STEPS[square, dr, dc]
                    if to and to not in state.board:
                        steps.append(f"{square}-{to}")
        return jumps or steps  # capturing is compulsory

    def turn(self, state):
        return state.turn

    def legal(self, state):
        if state.quiet >= CHECKERS_QUIET_LIMIT:
            return []
        return self.moves_for(state)

    def play(self, state, move):
        if move not in self.moves_for(state):
            raise ValueError("illegal move")
        squares = [int(s) for s in move.replace("x", "-").split("-")]
        board = dict(state.board)
        piece = board.pop(squares[0])
        captured = False
        for a, b in zip(squares, squares[1:]):
            ra, ca = checkers_coords(a)
            rb, cb = checkers_coords(b)
            if abs(ra - rb) == 2:
                board.pop(CHECKERS_SQUARE[(ra + rb) // 2, (ca + cb) // 2], None)
                captured = True
        end = squares[-1]
        if self.promotes(piece, end):
            piece = piece.upper()
        board[end] = piece
        quiet = 0 if captured or piece.islower() else state.quiet + 1
        return CheckersState(board, OTHER[state.turn], quiet)

    def outcome(self, state):
        if state.quiet >= CHECKERS_QUIET_LIMIT:
            return None, "the 40-move rule"
        if self.moves_for(state):
            return None
        mine = state.turn[0]
        how = "capturing every piece" if not any(p.lower() == mine for p in state.board.values()) else "leaving no moves"
        return OTHER[state.turn], how

    def snapshot(self, state):
        cells = []
        for row in range(8):
            for col in range(8):
                cells.append(state.board.get(CHECKERS_SQUARE.get((row, col)), "."))
        return "".join(cells)

    def score(self, state):
        left = {c: sum(1 for p in state.board.values() if p.lower() == c[0]) for c in ("black", "white")}
        return {"black": 12 - left["white"], "white": 12 - left["black"]}  # pieces each side has captured

    # computer opponent
    def ordered_moves(self, state):
        return sorted(self.moves_for(state), key=lambda m: -m.count("x"))

    def evaluate(self, state):
        mine = state.turn[0]
        result = self.outcome(state)
        if result:
            winner = result[0]
            return 0 if winner is None else (10000 if winner == state.turn else -10000)
        total = 0
        for square, piece in state.board.items():
            row, col = checkers_coords(square)
            if piece.isupper():
                value = 160 + (6 - abs(3.5 - row) - abs(3.5 - col))  # kings like the centre
            else:
                value = 100 + 3 * (row if piece == "b" else 7 - row)  # men like to advance
                if (piece == "b" and row == 0) or (piece == "w" and row == 7):
                    value += 8  # keeping the back row guarded stops enemy kings
            total += value if piece.lower() == mine else -value
        return total

    def bot_move(self, state, level):
        moves = self.legal(state)
        if len(moves) == 1:
            return moves[0]
        if level == 1:
            return random.choice(moves)
        depth, seconds = {2: (2, 0.3), 3: (4, 0.6), 4: (6, 1.2), 5: (10, 2.0)}[level]
        return negamax_search(self, state, depth, seconds)


# ---------------------------------------------------------------- go (9x9, area scoring, positional superko)

GO_SIZE = 9
GO_KOMI = 7.5
GO_LETTERS = "ABCDEFGHJ"  # go skips I
GO_MAX_PLIES = 250
GO_STAR_POINTS = {(2, 2), (2, 6), (6, 2), (6, 6), (4, 4)}
GO_NEIGHBOURS = [[j for j in (
    i - GO_SIZE if i >= GO_SIZE else None, i + GO_SIZE if i < GO_SIZE * (GO_SIZE - 1) else None,
    i - 1 if i % GO_SIZE else None, i + 1 if i % GO_SIZE < GO_SIZE - 1 else None) if j is not None]
    for i in range(GO_SIZE * GO_SIZE)]


class GoState:
    __slots__ = ("board", "turn", "captures", "passes", "seen", "plies")

    def __init__(self, board, turn, captures, passes, seen, plies):
        self.board, self.turn, self.captures, self.passes, self.seen, self.plies = \
            board, turn, captures, passes, seen, plies


def go_point(index):
    return f"{GO_LETTERS[index % GO_SIZE]}{GO_SIZE - index // GO_SIZE}"


def go_index(move):
    col = GO_LETTERS.index(move[0].upper())
    row = GO_SIZE - int(move[1:])
    if not (0 <= row < GO_SIZE):
        raise ValueError
    return row * GO_SIZE + col


def go_group(board, index):
    """(stones in the group, its liberties)."""
    colour, stones, liberties, todo = board[index], {index}, set(), [index]
    while todo:
        for n in GO_NEIGHBOURS[todo.pop()]:
            if board[n] == ".":
                liberties.add(n)
            elif board[n] == colour and n not in stones:
                stones.add(n)
                todo.append(n)
    return stones, liberties


class Go(Rules):
    kind, title, emoji = "go", "Go", "⚪"
    clock_minutes = 10  # a 9x9 game is about 40 moves each
    level_note = "1 = beginner · 3 = casual · 5 = strongest (still a light opponent)"

    def new(self):
        board = "." * (GO_SIZE * GO_SIZE)
        return GoState(board, "black", {"black": 0, "white": 0}, 0, frozenset([board]), 0)

    def place(self, state, index):
        """The board after a stone goes here, and how many it captures; raises ValueError if illegal."""
        if state.board[index] != ".":
            raise ValueError("that point is taken")
        mine, theirs = state.turn[0], OTHER[state.turn][0]
        board = list(state.board)
        board[index] = mine
        captured = 0
        for n in GO_NEIGHBOURS[index]:
            if board[n] == theirs:
                stones, liberties = go_group(board, n)
                if not liberties:
                    for s in stones:
                        board[s] = "."
                    captured += len(stones)
        if not go_group(board, index)[1]:
            raise ValueError("that would be suicide")
        board = "".join(board)
        if board in state.seen:
            raise ValueError("that repeats an earlier position (ko)")
        return board, captured

    def turn(self, state):
        return state.turn

    def legal(self, state):
        if self.outcome(state):
            return []
        moves = []
        for i in range(GO_SIZE * GO_SIZE):
            if state.board[i] == ".":
                try:
                    self.place(state, i)
                    moves.append(go_point(i))
                except ValueError:
                    pass
        return moves + ["pass"]

    def play(self, state, move):
        if self.outcome(state):
            raise ValueError("the game is over")
        if move == "pass":
            return GoState(state.board, OTHER[state.turn], state.captures, state.passes + 1, state.seen, state.plies + 1)
        try:
            index = go_index(move)
        except (ValueError, IndexError):
            raise ValueError("bad move")
        board, captured = self.place(state, index)
        captures = dict(state.captures)
        captures[state.turn] += captured
        return GoState(board, OTHER[state.turn], captures, 0, state.seen | {board}, state.plies + 1)

    def area(self, board):
        """Stones plus empty regions that touch only one colour (Tromp-Taylor scoring)."""
        score = {"b": board.count("b"), "w": board.count("w")}
        seen = set()
        for i, point in enumerate(board):
            if point != "." or i in seen:
                continue
            region, borders, todo = {i}, set(), [i]
            while todo:
                for n in GO_NEIGHBOURS[todo.pop()]:
                    if board[n] == "." and n not in region:
                        region.add(n)
                        todo.append(n)
                    elif board[n] != ".":
                        borders.add(board[n])
            seen |= region
            if len(borders) == 1:
                score[borders.pop()] += len(region)
        return score["b"], score["w"] + GO_KOMI

    def outcome(self, state):
        if state.passes < 2 and state.plies < GO_MAX_PLIES:
            return None
        black, white = self.area(state.board)
        margin = abs(black - white)
        return ("black" if black > white else "white"), f"{margin:g} points ({black:g}–{white:g} with {GO_KOMI:g} komi)"

    def snapshot(self, state):
        return state.board

    def score(self, state):
        return dict(state.captures)  # stones each side has captured

    # computer opponent: quick tactical judgement plus a feel for territory (no deep reading)
    def influence(self, board):
        """Rough territory estimate for black minus white: each empty point goes to the closer colour."""
        dist = {}
        for colour in "bw":
            d = [99] * len(board)
            frontier = [i for i, p in enumerate(board) if p == colour]
            for i in frontier:
                d[i] = 0
            step = 0
            while frontier and step < 4:
                step += 1
                nxt = []
                for i in frontier:
                    for n in GO_NEIGHBOURS[i]:
                        if board[n] == "." and d[n] > step:
                            d[n] = step
                            nxt.append(n)
                frontier = nxt
            dist[colour] = d
        total = 0
        for i, p in enumerate(board):
            if p == "b":
                total += 1
            elif p == "w":
                total -= 1
            elif dist["b"][i] < dist["w"][i]:
                total += 1
            elif dist["w"][i] < dist["b"][i]:
                total -= 1
        return total

    def is_own_eye(self, board, index, mine):
        return all(board[n] == mine for n in GO_NEIGHBOURS[index])

    def bot_move(self, state, level):
        mine, theirs = state.turn[0], OTHER[state.turn][0]
        black, white = self.area(state.board)
        ahead = (black - white) if mine == "b" else (white - black)
        if state.passes == 1 and ahead > 0:
            return "pass"  # opponent passed and we're winning: end the game
        noise = {1: 40, 2: 18, 3: 8, 4: 4, 5: 2}[level]
        deadline = time.time() + {1: 0.3, 2: 0.6, 3: 1.0, 4: 1.5, 5: 2.0}[level]
        stones = state.board.count("b") + state.board.count("w")
        candidates = []
        for i in range(GO_SIZE * GO_SIZE):
            if state.board[i] != "." or self.is_own_eye(state.board, i, mine):
                continue
            try:
                board, captured = self.place(state, i)
            except ValueError:
                continue
            score = 60 * captured
            own_libs = len(go_group(board, i)[1])
            if own_libs == 1 and not captured:
                score -= 120  # walking into atari
            for n in GO_NEIGHBOURS[i]:
                if state.board[n] == mine:  # rescuing one of our groups in atari
                    group, libs = go_group(state.board, n)
                    if len(libs) == 1 and own_libs >= 2:
                        score += 50 * len(group)
                elif board[n] == theirs:  # putting theirs in atari
                    group, libs = go_group(board, n)
                    if len(libs) == 1:
                        score += 15 * len(group)
            row, col = divmod(i, GO_SIZE)
            line = min(row, col, GO_SIZE - 1 - row, GO_SIZE - 1 - col)
            if stones < 10:  # opening: third line and star points
                score += {0: -25, 1: -8, 2: 12, 3: 6, 4: 4}[line] + (6 if (row, col) in GO_STAR_POINTS else 0)
            elif line == 0:
                score -= 6
            candidates.append([score + random.random() * noise, i, board])
        if not candidates:
            return "pass"
        if level >= 3:  # add how the territory estimate changes, best candidates first, within the time budget
            before = self.influence(state.board)
            sign = 1 if mine == "b" else -1
            for c in sorted(candidates, key=lambda c: -c[0]):
                if time.time() > deadline:
                    break
                c[0] += (self.influence(c[2]) - before) * sign * (1.5 if level >= 4 else 1)
        best = max(candidates, key=lambda c: c[0])
        if best[0] < -40 and state.plies > 30:
            return "pass"  # nothing useful left to play
        return go_point(best[1])


# ---------------------------------------------------------------- games on points joined by lines (alquerque, 16 beads)

def alquerque_lines(n, row0=0):
    """Rows, columns and the alquerque diagonals (through points whose row + column is even) of an n x n grid."""
    rows = [[(r, c) for c in range(n)] for r in range(row0, row0 + n)]
    cols = [[(r, c) for r in range(row0, row0 + n)] for c in range(n)]
    diagonals = []
    for k in range(-(n - 1), n):
        if k % 2 == 0:
            diagonals.append([(r, r - row0 + k) for r in range(row0, row0 + n) if 0 <= r - row0 + k < n])
    for k in range(0, 2 * n - 1):
        if k % 2 == 0:
            diagonals.append([(r, k - (r - row0)) for r in range(row0, row0 + n) if 0 <= k - (r - row0) < n])
    return [line for line in rows + cols + diagonals if len(line) > 1]


def grid_lines(rows, cols):
    """Rows, columns and alquerque-style diagonals (through points whose row + column is even) of a grid."""
    lines = [[(r, c) for c in range(cols)] for r in range(rows)] + [[(r, c) for r in range(rows)] for c in range(cols)]
    for k in range(-(rows - 1), cols):
        if k % 2 == 0:
            lines.append([(r, r + k) for r in range(rows) if 0 <= r + k < cols])
    for k in range(0, rows + cols - 1):
        if k % 2 == 0:
            lines.append([(r, k - r) for r in range(rows) if 0 <= k - r < cols])
    return [line for line in lines if len(line) > 1]


def sixteen_beads_lines():
    """A 5x5 alquerque square (rows 0-4) with a triangle of six points beyond the middle of each end."""
    lines = []
    for line in alquerque_lines(5):
        if line == [(r, 2) for r in range(5)]:
            line = [(-2, 2), (-1, 2)] + line + [(5, 2), (6, 2)]  # the middle column runs through both triangles
        elif line == [(0, 2), (1, 3), (2, 4)]:
            line = [(-2, 0), (-1, 1)] + line
        elif line == [(0, 2), (1, 1), (2, 0)]:
            line = [(-2, 4), (-1, 3)] + line
        elif line == [(2, 0), (3, 1), (4, 2)]:
            line = line + [(5, 3), (6, 4)]
        elif line == [(2, 4), (3, 3), (4, 2)]:
            line = line + [(5, 1), (6, 0)]
        lines.append(line)
    lines += [[(-1, 1), (-1, 2), (-1, 3)], [(-2, 0), (-2, 2), (-2, 4)],
              [(5, 1), (5, 2), (5, 3)], [(6, 0), (6, 2), (6, 4)]]
    return lines


class LineBoard:
    """Points (numbered 1.. in reading order) joined by straight lines: who's next to whom, what can be jumped."""

    def __init__(self, lines):
        coords = sorted({p for line in lines for p in line})
        self.coords = coords
        self.index = {p: i for i, p in enumerate(coords)}
        self.lines = [[self.index[p] for p in line] for line in lines]
        self.steps = {i: [] for i in range(len(coords))}  # neighbours
        self.jumps = {i: [] for i in range(len(coords))}  # (over, land)
        self.rays = {i: [] for i in range(len(coords))}  # points along each line, nearest first
        self.between = {}  # (a, b) on one line -> points strictly between them
        for line in self.lines:
            for seq in (line, line[::-1]):
                for i, a in enumerate(seq):
                    if i + 1 < len(seq):
                        self.steps[a].append(seq[i + 1])
                        self.rays[a].append(seq[i + 1:])
                    if i + 2 < len(seq):
                        self.jumps[a].append((seq[i + 1], seq[i + 2]))
                    for j in range(i + 1, len(seq)):
                        self.between[a, seq[j]] = seq[i + 1:j]
        self.top, self.bottom = coords[0][0], coords[-1][0]

    def row(self, i):
        return self.coords[i][0]

    def segments(self):
        return sorted({tuple(sorted((a, b))) for line in self.lines for a, b in zip(line, line[1:])})


class LineGameState:
    __slots__ = ("board", "turn", "quiet")

    def __init__(self, board, turn, quiet):
        self.board, self.turn, self.quiet = board, turn, quiet  # board: list of "." "b" "w" "B" "W" per point


class LineGame(Rules):
    """Alquerque-family games: pieces move along the lines and capture by jumping over an enemy piece."""
    men_move = "any"  # "any" direction, "forward_sideways", or "forward"
    frozen_back_row = False  # men on the far row may only capture
    kings = False  # men reaching the far row become flying kings
    compulsory = True  # must capture when possible (and finish the whole chain)
    quiet_limit = 60  # 30 moves each without a capture: draw

    def __init__(self, lines, black_rows, white_rows, middle_split=None):
        self.grid = LineBoard(lines)
        board = ["."] * len(self.grid.coords)
        for i, (r, c) in enumerate(self.grid.coords):
            if r in black_rows:
                board[i] = "b"
            elif r in white_rows:
                board[i] = "w"
            elif middle_split and r == middle_split[0]:  # the middle row is shared: each side gets half
                board[i] = "w" if c < middle_split[1] else "b" if c > middle_split[1] else "."
        self.start = board
        self.pieces = board.count("b")

    def new(self):
        return LineGameState(self.start[:], "black", 0)

    def turn(self, state):
        return state.turn

    def name(self, i):
        return str(i + 1)

    def far_row(self, piece):
        return self.grid.bottom if piece.lower() == "b" else self.grid.top

    def step_ok(self, piece, a, b):
        if piece.isupper() or self.men_move == "any":
            return True
        dr = self.grid.row(b) - self.grid.row(a)
        forward = dr > 0 if piece == "b" else dr < 0
        return forward or (dr == 0 and self.men_move == "forward_sideways")

    def capture_options(self, board, pos, piece, taken, start):
        """(captured point, [landing points]) for one more jump from pos."""
        mine = piece.lower()
        free = lambda p: board[p] == "." or p == start
        options = []
        if piece.isupper() and self.kings:  # flying king: any distance up to the enemy, any free point beyond
            for ray in self.grid.rays[pos]:
                k = 0
                while k < len(ray) and free(ray[k]) and ray[k] not in taken:
                    k += 1
                if k < len(ray) and ray[k] not in taken and board[ray[k]] != "." and board[ray[k]].lower() != mine:
                    lands = []
                    for p in ray[k + 1:]:
                        if not free(p) or p in taken:
                            break
                        lands.append(p)
                    if lands:
                        options.append((ray[k], lands))
            return options
        for over, land in self.grid.jumps[pos]:
            if board[over] != "." and board[over].lower() != mine and over not in taken and free(land):
                options.append((over, [land]))
        return options

    def capture_paths(self, board, start, piece):
        paths = []

        def extend(pos, path, taken):
            grew = False
            for over, lands in self.capture_options(board, pos, piece, taken, start):
                for land in lands:
                    grew = True
                    extend(land, path + [land], taken | {over})
            if len(path) > 1 and (not grew or not self.compulsory):
                paths.append(path)  # optional captures may stop after any jump

        extend(start, [start], frozenset())
        return paths

    def moves_for(self, state):
        mine = state.turn[0]
        captures, steps = [], []
        for i, piece in enumerate(state.board):
            if piece.lower() != mine:
                continue
            for path in self.capture_paths(state.board, i, piece):
                captures.append("x".join(self.name(p) for p in path))
            if self.frozen_back_row and piece.islower() and self.grid.row(i) == self.far_row(piece):
                continue
            if piece.isupper() and self.kings:
                for ray in self.grid.rays[i]:
                    for p in ray:
                        if state.board[p] != ".":
                            break
                        steps.append(f"{self.name(i)}-{self.name(p)}")
            else:
                steps += [f"{self.name(i)}-{self.name(b)}" for b in self.grid.steps[i]
                          if state.board[b] == "." and self.step_ok(piece, i, b)]
        if captures and self.compulsory:
            return captures
        return captures + steps

    def legal(self, state):
        if state.quiet >= self.quiet_limit:
            return []
        return self.moves_for(state)

    def play(self, state, move):
        if move not in self.legal(state):
            raise ValueError("illegal move")
        points = [int(p) - 1 for p in move.replace("x", "-").split("-")]
        board = state.board[:]
        piece = board[points[0]]
        board[points[0]] = "."
        captured = "x" in move
        if captured:
            for a, b in zip(points, points[1:]):
                for p in self.grid.between[a, b]:
                    board[p] = "."
        end = points[-1]
        if self.kings and piece.islower() and self.grid.row(end) == self.far_row(piece):
            piece = piece.upper()
        board[end] = piece
        return LineGameState(board, OTHER[state.turn], 0 if captured else state.quiet + 1)

    def outcome(self, state):
        if state.quiet >= self.quiet_limit:
            return None, f"{self.quiet_limit // 2} moves each without a capture"
        if self.moves_for(state):
            return None
        mine = state.turn[0]
        how = "capturing every piece" if not any(p.lower() == mine for p in state.board) else "leaving no moves"
        return OTHER[state.turn], how

    def snapshot(self, state):
        return "".join(state.board)

    def score(self, state):
        left = {c: sum(1 for p in state.board if p.lower() == c[0]) for c in ("black", "white")}
        return {"black": self.pieces - left["white"], "white": self.pieces - left["black"]}  # pieces captured

    def layout(self):
        return {"points": [[c, r] for r, c in self.grid.coords], "segments": self.grid.segments(), "pieces": self.pieces}

    # computer opponent
    def ordered_moves(self, state):
        return sorted(self.moves_for(state), key=lambda m: -m.count("x"))

    def evaluate(self, state):
        result = self.outcome(state)
        if result:
            return 0 if result[0] is None else (10000 if result[0] == state.turn else -10000)
        mine, total = state.turn[0], 0
        span = self.grid.bottom - self.grid.top
        for i, piece in enumerate(state.board):
            if piece == ".":
                continue
            if piece.isupper():
                value = 300
            else:
                value = 100
                if self.kings:  # heading for the far row
                    advanced = self.grid.row(i) - self.grid.top if piece == "b" else self.grid.bottom - self.grid.row(i)
                    value += 20 * advanced // max(span, 1)
            value += 2 * len(self.grid.steps[i])  # well-connected points are stronger
            total += value if piece.lower() == mine else -value
        return total

    def bot_move(self, state, level):
        moves = self.legal(state)
        if len(moves) == 1:
            return moves[0]
        if level == 1:
            return random.choice(moves)
        depth, seconds = {2: (2, 0.4), 3: (3, 0.8), 4: (5, 1.4), 5: (8, 2.0)}[level]
        return negamax_search(self, state, depth, seconds)


class Alquerque(LineGame):
    kind, title, emoji, clock_minutes = "alquerque", "Alquerque (5×5)", "⚪", 5  # about 15 moves each
    level_note = "1 = beginner · 3 = casual · 5 = strongest"
    men_move, frozen_back_row, size = "forward_sideways", True, 5

    def __init__(self):
        super().__init__(alquerque_lines(5), black_rows={0, 1}, white_rows={3, 4}, middle_split=(2, 2))


class BigAlquerque(LineGame):
    kind, title, emoji, clock_minutes = "alquerque9", "Alquerque (9×9)", "⚪", 10  # about 60 moves each
    level_note = "1 = beginner · 3 = casual · 5 = strongest"
    men_move, kings, size = "forward", True, 9

    def __init__(self):
        super().__init__(alquerque_lines(9), black_rows={0, 1, 2, 3}, white_rows={5, 6, 7, 8}, middle_split=(4, 4))


class SixteenBeads(LineGame):
    kind, title, emoji, clock_minutes = "beads", "16 Beads", "🔴", 10
    level_note = "1 = beginner · 3 = casual · 5 = strongest"
    compulsory = False

    def __init__(self):
        super().__init__(sixteen_beads_lines(), black_rows={-2, -1, 0, 1}, white_rows={3, 4, 5, 6})


class FanoronaState:
    __slots__ = ("board", "turn", "quiet", "plies")

    def __init__(self, board, turn, quiet, plies):
        self.board, self.turn, self.quiet, self.plies = board, turn, quiet, plies


class Fanorona(LineGame):
    """Fanorona: capture by approaching or withdrawing from a line of enemy pieces; a capturing piece may keep
    going, but not twice in the same direction or back to a point it has visited this turn.
    A move is written as the points it passes, each step marked "a" (approach), "w" (withdrawal) or "-" (no capture),
    e.g. "23a24w25"."""
    kind, title, emoji, clock_minutes, first = "fanorona", "Fanorona", "⚫", 10, "white"
    level_note = "1 = beginner · 3 = casual · 5 = strongest"
    COLUMNS = "abcdefghi"

    def __init__(self):
        super().__init__(grid_lines(5, 9), black_rows={0, 1}, white_rows={3, 4})
        for i, (r, c) in enumerate(self.grid.coords):  # middle row: alternating, centre empty
            if r == 2:
                self.start[i] = "." if c == 4 else ("b" if (c < 4) == (c % 2 == 0) else "w")
        self.pieces = self.start.count("b")
        self.at = {p: i for i, p in enumerate(self.grid.coords)}

    def new(self):
        return FanoronaState(self.start[:], "white", 0, 0)

    def line_from(self, board, point, direction, enemy):
        """The run of enemy pieces starting at point and continuing in direction."""
        taken, (r, c) = [], self.grid.coords[point] if point is not None else (None, None)
        while point is not None and board[point] == enemy:
            taken.append(point)
            r, c = r + direction[0], c + direction[1]
            point = self.at.get((r, c))
        return taken

    def step_captures(self, board, a, b, enemy):
        """{"a": [...], "w": [...]} pieces captured by approach / withdrawal when moving a -> b (empty lists dropped)."""
        (ra, ca), (rb, cb) = self.grid.coords[a], self.grid.coords[b]
        d = (rb - ra, cb - ca)
        result = {}
        ahead = self.at.get((rb + d[0], cb + d[1]))
        if ahead is not None and board[ahead] == enemy:
            result["a"] = self.line_from(board, ahead, d, enemy)
        behind = self.at.get((ra - d[0], ca - d[1]))
        if behind is not None and board[behind] == enemy:
            result["w"] = self.line_from(board, behind, (-d[0], -d[1]), enemy)
        return result

    def moves_for(self, state):
        mine, enemy = state.turn[0], OTHER[state.turn][0]
        captures, paika = [], []
        for start, piece in enumerate(state.board):
            if piece != mine:
                continue

            def extend(board, pos, visited, last_dir, text):
                for nxt in self.grid.steps[pos]:
                    if board[nxt] != "." or nxt in visited:
                        continue
                    (r0, c0), (r1, c1) = self.grid.coords[pos], self.grid.coords[nxt]
                    d = (r1 - r0, c1 - c0)
                    if d == last_dir:
                        continue
                    for how, taken in self.step_captures(board, pos, nxt, enemy).items():
                        move = f"{text}{how}{nxt + 1}"
                        captures.append(move)
                        if state.plies == 0:
                            continue  # the opening turn is a single capture
                        after = board[:]
                        after[pos], after[nxt] = ".", mine
                        for t in taken:
                            after[t] = "."
                        extend(after, nxt, visited | {nxt}, d, move)

            extend(state.board, start, {start}, None, str(start + 1))
            paika += [f"{start + 1}-{b + 1}" for b in self.grid.steps[start] if state.board[b] == "."]
        return captures or paika

    def play(self, state, move):
        if move not in self.legal(state):
            raise ValueError("illegal move")
        board, enemy = state.board[:], OTHER[state.turn][0]
        points = [int(p) - 1 for p in re.split(r"[aw-]", move)]
        kinds = re.findall(r"[aw-]", move)
        captured = False
        for (a, b), how in zip(zip(points, points[1:]), kinds):
            taken = self.step_captures(board, a, b, enemy).get(how, []) if how != "-" else []
            board[b], board[a] = board[a], "."
            for t in taken:
                board[t] = "."
            captured = captured or bool(taken)
        return FanoronaState(board, OTHER[state.turn], 0 if captured else state.quiet + 1, state.plies + 1)

    def label(self, state, move):
        name = lambda n: self.COLUMNS[self.grid.coords[int(n) - 1][1]] + str(5 - self.grid.coords[int(n) - 1][0])
        points = re.split(r"[aw-]", move)
        kinds = re.findall(r"[aw-]", move)
        text = name(points[0])
        for point, how in zip(points[1:], kinds):
            text += "-" + name(point) + {"-": "", "a": "A", "w": "W"}[how]  # A = approach, W = withdrawal
        return text

    def ordered_moves(self, state):
        return sorted(self.moves_for(state), key=lambda m: -len(m))


class DraughtsState:
    __slots__ = ("board", "turn", "quiet")

    def __init__(self, board, turn, quiet):
        self.board, self.turn, self.quiet = board, turn, quiet  # board: 64 chars, top row first


class Draughts(Rules):
    """Turkish draughts: men move one square forward or sideways and capture the same way; kings fly along
    ranks and files. Captured pieces vanish at once, capturing is compulsory and the longest capture must be taken."""
    kind, title, emoji, clock_minutes, first = "draughts", "Turkish draughts", "⛀", 10, "white"
    level_note = "1 = beginner · 3 = casual · 5 = strongest"
    quiet_limit = 60
    ORTHOGONAL = [(-1, 0), (1, 0), (0, -1), (0, 1)]

    def new(self):
        board = ["."] * 64
        for c in range(8):
            board[8 + c] = board[16 + c] = "b"
            board[40 + c] = board[48 + c] = "w"
        return DraughtsState(board, "white", 0)

    @staticmethod
    def name(i):
        return "abcdefgh"[i % 8] + str(8 - i // 8)

    @staticmethod
    def index(name):
        return "abcdefgh".index(name[0]) + (8 - int(name[1:])) * 8

    def man_dirs(self, piece):
        return [(1 if piece == "b" else -1, 0), (0, -1), (0, 1)]

    def captures(self, board, start, piece):
        """Every capture sequence from start: [(path of squares, number captured)]."""
        enemy = "w" if piece.lower() == "b" else "b"
        results = []

        def extend(board, pos, path, count, last_dir):
            grew = False
            r0, c0 = divmod(pos, 8)
            for dr, dc in (self.ORTHOGONAL if piece.isupper() else self.man_dirs(piece)):
                if last_dir and (dr, dc) == (-last_dir[0], -last_dir[1]):
                    continue  # no turning back
                r, c = r0 + dr, c0 + dc
                if piece.isupper():  # fly over empty squares to the enemy
                    while 0 <= r < 8 and 0 <= c < 8 and board[r * 8 + c] == ".":
                        r, c = r + dr, c + dc
                if not (0 <= r < 8 and 0 <= c < 8) or board[r * 8 + c].lower() != enemy:
                    continue
                over = r * 8 + c
                lands = []
                r, c = r + dr, c + dc
                while 0 <= r < 8 and 0 <= c < 8 and board[r * 8 + c] == ".":
                    lands.append(r * 8 + c)
                    if piece.islower():
                        break  # men land right behind
                    r, c = r + dr, c + dc
                for land in lands:
                    after = board[:]
                    after[over], after[pos], after[land] = ".", ".", piece  # captured pieces vanish at once
                    grew = True
                    extend(after, land, path + [land], count + 1, (dr, dc))
            if not grew and count:
                results.append((path, count))

        extend(board, start, [start], 0, None)
        return results

    def moves_for(self, state):
        mine = state.turn[0]
        captures, steps = [], []
        for i, piece in enumerate(state.board):
            if piece.lower() != mine:
                continue
            captures += self.captures(state.board, i, piece)
            r0, c0 = divmod(i, 8)
            for dr, dc in (self.ORTHOGONAL if piece.isupper() else self.man_dirs(piece)):
                r, c = r0 + dr, c0 + dc
                while 0 <= r < 8 and 0 <= c < 8 and state.board[r * 8 + c] == ".":
                    steps.append(f"{self.name(i)}-{self.name(r * 8 + c)}")
                    if piece.islower():
                        break
                    r, c = r + dr, c + dc
        if captures:
            most = max(count for _, count in captures)  # the longest capture is compulsory
            return sorted({"x".join(self.name(p) for p in path) for path, count in captures if count == most})
        return steps

    def turn(self, state):
        return state.turn

    def legal(self, state):
        if state.quiet >= self.quiet_limit:
            return []
        return self.moves_for(state)

    def play(self, state, move):
        if move not in self.legal(state):
            raise ValueError("illegal move")
        squares = [self.index(n) for n in move.replace("x", "-").split("-")]
        board = state.board[:]
        piece = board[squares[0]]
        board[squares[0]] = "."
        for a, b in zip(squares, squares[1:]):
            if "x" in move:  # remove the one enemy piece between a and b
                ra, ca = divmod(a, 8)
                rb, cb = divmod(b, 8)
                dr, dc = (rb > ra) - (rb < ra), (cb > ca) - (cb < ca)
                r, c = ra + dr, ca + dc
                while (r, c) != (rb, cb):
                    board[r * 8 + c] = "."
                    r, c = r + dr, c + dc
        end = squares[-1]
        if piece.islower() and end // 8 == (7 if piece == "b" else 0):
            piece = piece.upper()
        board[end] = piece
        quiet = 0 if "x" in move else state.quiet + 1
        return DraughtsState(board, OTHER[state.turn], quiet)

    def outcome(self, state):
        if state.quiet >= self.quiet_limit:
            return None, f"{self.quiet_limit // 2} moves each without a capture"
        if self.moves_for(state):
            return None
        mine = state.turn[0]
        how = "capturing every piece" if not any(p.lower() == mine for p in state.board) else "leaving no moves"
        return OTHER[state.turn], how

    def snapshot(self, state):
        return "".join(state.board)

    def score(self, state):
        left = {c: sum(1 for p in state.board if p.lower() == c[0]) for c in ("black", "white")}
        return {"black": 16 - left["white"], "white": 16 - left["black"]}

    # computer opponent
    def ordered_moves(self, state):
        return sorted(self.moves_for(state), key=lambda m: -m.count("x"))

    def evaluate(self, state):
        result = self.outcome(state)
        if result:
            return 0 if result[0] is None else (10000 if result[0] == state.turn else -10000)
        mine, total = state.turn[0], 0
        for i, piece in enumerate(state.board):
            if piece == ".":
                continue
            row = i // 8
            value = 400 if piece.isupper() else 100 + 4 * (row if piece == "b" else 7 - row)
            total += value if piece.lower() == mine else -value
        return total

    def bot_move(self, state, level):
        moves = self.legal(state)
        if len(moves) == 1:
            return moves[0]
        if level == 1:
            return random.choice(moves)
        depth, seconds = {2: (2, 0.4), 3: (4, 0.8), 4: (6, 1.4), 5: (8, 2.0)}[level]
        return negamax_search(self, state, depth, seconds)


GAMES = {rules.kind: rules for rules in (Chess(), Othello(), Checkers(), Go(), Alquerque(), BigAlquerque(),
                                       SixteenBeads(), Fanorona(), Draughts())}


# ---------------------------------------------------------------- dice games for 2-6 people (no computer opponent)
# State is a plain dict (stored as JSON). Dice come from secrets (the operating system's cryptographic random
# source), so nobody - not even the server - can predict or influence a roll.

import secrets


def roll_die():
    return secrets.randbelow(6) + 1


class PartyGame:
    kind = title = emoji = ""
    min_players, max_players = 2, 4

    def next_turn(self, state):
        state["turn"] = (state["turn"] + 1) % len(state["players_done"])
        state["phase"] = "roll"


SNAKES = {16: 6, 47: 26, 49: 11, 56: 53, 62: 19, 64: 60, 87: 24, 93: 73, 95: 75, 98: 78}
LADDERS = {1: 38, 4: 14, 9: 31, 21: 42, 28: 84, 36: 44, 51: 67, 71: 91, 80: 100}


class SnakesAndLadders(PartyGame):
    kind, title, emoji, min_players, max_players = "snakes", "Snakes and Ladders", "🐍", 2, 6

    def new(self, players):
        return {"pos": [0] * players, "turn": 0, "phase": "roll", "dice": None, "rolls": 0, "winner": None,
                "players_done": [False] * players}

    def roll(self, state, dice=None):
        """Roll for the player to move. Returns a list of event strings ("{0}" stands for that player's name)."""
        dice = dice or roll_die()
        me = state["turn"]
        state["dice"], state["rolls"] = dice, state["rolls"] + 1
        here, target = state["pos"][me], state["pos"][me] + dice
        if target > 100:
            events = [f"{{{me}}} rolled {dice} but needs exactly {100 - here} to finish"]
        else:
            events = [f"{{{me}}} rolled {dice}: {here or 'start'} → {target}"]
            if target in LADDERS:
                events.append(f"🪜 {{{me}}} climbed a ladder to {LADDERS[target]}")
                target = LADDERS[target]
            elif target in SNAKES:
                events.append(f"🐍 {{{me}}} slid down a snake to {SNAKES[target]}")
                target = SNAKES[target]
            state["pos"][me] = target
            if target == 100:
                state["winner"] = me
                events.append(f"🏆 {{{me}}} reached 100!")
                return events
        self.next_turn(state)
        return events

    def auto(self, state):
        return self.roll(state)

    def layout(self):
        return {"snakes": SNAKES, "ladders": LADDERS}


# Ludo on the usual 15 x 15 board. Track squares are (row, col), clockwise from Red's start; each colour
# starts 13 squares further on, walks 51 squares, then 5 squares up its own home column and into home (56).
def ludo_track():
    path = [(6, c) for c in range(1, 6)] + [(r, 6) for r in range(5, -1, -1)] + [(0, 7)] + \
           [(r, 8) for r in range(0, 6)] + [(6, c) for c in range(9, 15)] + [(7, 14)] + \
           [(8, c) for c in range(14, 8, -1)] + [(r, 8) for r in range(9, 15)] + [(14, 7)] + \
           [(r, 6) for r in range(14, 8, -1)] + [(8, c) for c in range(5, -1, -1)] + [(7, 0), (6, 0)]
    return path


LUDO_TRACK = ludo_track()
LUDO_COLOURS = ["red", "green", "yellow", "blue"]
LUDO_HOME_COLUMNS = {
    "red": [(7, c) for c in range(1, 6)], "green": [(r, 7) for r in range(1, 6)],
    "yellow": [(7, c) for c in range(13, 8, -1)], "blue": [(r, 7) for r in range(13, 8, -1)],
}
LUDO_OFFSET = {colour: 13 * i for i, colour in enumerate(LUDO_COLOURS)}
LUDO_SAFE = {0, 8, 13, 21, 26, 34, 39, 47}  # start squares and stars
LUDO_HOME = 56


class Ludo(PartyGame):
    kind, title, emoji, min_players, max_players = "ludo", "Ludo", "🎲", 2, 4

    def colours(self, players):
        return {2: ["red", "yellow"], 3: ["red", "green", "yellow"], 4: LUDO_COLOURS}[players]

    def new(self, players):
        return {"tokens": [[-1] * 4 for _ in range(players)], "colours": self.colours(players), "turn": 0,
                "phase": "roll", "dice": None, "sixes": 0, "winner": None, "players_done": [False] * players}

    @staticmethod
    def square(colour, progress):
        """Track index (0-51) of a token, or None if it's in base, its home column or home."""
        return (LUDO_OFFSET[colour] + progress) % 52 if 0 <= progress <= 50 else None

    def movable(self, state, player):
        dice = state["dice"]
        return [t for t, p in enumerate(state["tokens"][player])
                if (p == -1 and dice == 6) or (0 <= p and p + dice <= LUDO_HOME)]

    def roll(self, state, dice=None):
        dice = dice or roll_die()
        me = state["turn"]
        state["dice"] = dice
        events = [f"{{{me}}} rolled {dice}"]
        if dice == 6:
            state["sixes"] += 1
            if state["sixes"] == 3:
                events.append(f"{{{me}}} rolled three 6s in a row and loses the turn")
                self.pass_turn(state)
                return events
        options = self.movable(state, me)
        if not options:
            events.append(f"{{{me}}} can't move")
            self.pass_turn(state)
        elif len(options) == 1 or len({state["tokens"][me][t] for t in options}) == 1:
            events += self.move(state, options[0])  # only one real choice: play it straight away
        else:
            state["phase"] = "move"
        return events

    def pass_turn(self, state):
        state["sixes"] = 0
        self.next_turn(state)

    def move(self, state, token):
        me, dice = state["turn"], state["dice"]
        if state["phase"] not in ("move", "roll") or token not in self.movable(state, me):
            raise ValueError("that token can't move")
        colour = state["colours"][me]
        tokens = state["tokens"][me]
        events, again = [], dice == 6
        tokens[token] = 0 if tokens[token] == -1 else tokens[token] + dice
        if tokens[token] == LUDO_HOME:
            events.append(f"🏠 {{{me}}} brought a token home")
            again = True
        square = self.square(colour, tokens[token])
        if square is not None and square not in LUDO_SAFE:
            for other, their in enumerate(state["tokens"]):
                if other == me:
                    continue
                for t, p in enumerate(their):
                    if self.square(state["colours"][other], p) == square:
                        their[t] = -1
                        events.append(f"💥 {{{me}}} knocked {{{other}}}'s token back to base")
                        again = True
        if all(p == LUDO_HOME for p in tokens):
            state["winner"] = me
            events.append(f"🏆 {{{me}}} got all four tokens home!")
            return events
        if again:
            state["phase"] = "roll"  # a 6, a capture or a token home: roll again
        else:
            self.pass_turn(state)
        return events

    def auto(self, state):
        """The player ran out of time: roll for them, or move their most advanced token."""
        if state["phase"] == "roll":
            return self.roll(state)
        best = max(self.movable(state, state["turn"]), key=lambda t: state["tokens"][state["turn"]][t])
        return self.move(state, best)

    def layout(self):
        return {"track": LUDO_TRACK, "home_columns": LUDO_HOME_COLUMNS, "offset": LUDO_OFFSET, "safe": sorted(LUDO_SAFE)}


PARTY_GAMES = {rules.kind: rules for rules in (SnakesAndLadders(), Ludo())}
