"""Rules and light computer opponents for the board games Laden hosts: chess, othello, checkers and go.

Every game uses the same interface, so the bot can store a game as a plain list of move strings and
replay it. Colours are "white" and "black"; boards are sent to the Mini App as one character per square.
The opponents are plain Python with a time budget per move, so they stay quick on a tiny server."""

import random
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


GAMES = {rules.kind: rules for rules in (Chess(), Othello(), Checkers(), Go())}
