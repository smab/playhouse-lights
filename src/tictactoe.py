
import random
import sys
import time

from PyQt4 import QtGui, QtCore

import playhouse

usernames = {
    "001788182e78": "25116fda765dc973fae9b4611ec2fb3",
    "00178811f9c2": "newdeveloper",
    "001788182c73": "3PeT4zaFlqOtf2Pr"
}

grid = [
    [("001788182c73", 1), ("001788182c73", 2), ("001788182c73", 3)],
    [("001788182e78", 3), ("001788182e78", 2), ("001788182e78", 1)],
    [("00178811f9c2", 2), ("00178811f9c2", 3), ("00178811f9c2", 1)]
]

ips = {"130.237.228.161:80", "130.237.228.58:80", "130.237.228.213:80"}

lg = playhouse.LightGrid(usernames, grid, ips, buffered=False)

def main():
    for i in range(3):
        for j in range(3):
            lg.set_state(i, j, sat=0, hue=0, bri=0)
    
    app = QtGui.QApplication(sys.argv)
    
    window = QtGui.QMainWindow()
    window.setWindowTitle("Tic tac toe")
    
    widget = QtGui.QWidget()
    layout = QtGui.QGridLayout()
    
    for row in range(3):
        for column in range(3):
            def clicked(row, column):
                def action():
                    do_turn(column, row)
                return action
            
            button = QtGui.QPushButton("{}:{}".format(row, column))
            button.clicked.connect(clicked(row, column))
            layout.addWidget(button, row, column)
    
    widget.setLayout(layout)
    window.setCentralWidget(widget)
    
    window.show()
    
    app.exec()


# ====== #

player = 0
colors = [0, 45000]

board = [[-1, -1, -1],
         [-1, -1, -1],
         [-1, -1, -1]]

def reset():
    global player, board
    for i in range(3):
        for j in range(3):
            lg.set_state(i, j, hue=0, sat=0)
    board = [[-1, -1, -1],
             [-1, -1, -1],
             [-1, -1, -1]]
    player = 0

def do_turn(x, y):
    global player
    if board[y][x] != -1:
        return
    board[y][x] = player
    lg.set_state(x, y, hue=colors[player], sat=255)
    
    winner_lamps = set()
    for configuration in [[(y, 0), (y, 1), (y, 2)],
                          [(0, x), (1, x), (2, x)],
                          [(0, 0), (1, 1), (2, 2)],
                          [(0, 2), (1, 1), (2, 0)]]:
        if all(board[i][j] == player for i, j in configuration):
            winner_lamps.update(configuration)
    
    if len(winner_lamps) > 0:
        time.sleep(0.5)
        for i, j in winner_lamps:
            lg.set_state(j, i, alert="lselect")
        time.sleep(5)
        reset()
        return
    if all(all(i != -1 for i in j) for j in board):
        reset()
        return
    
    player = 1 - player

if __name__ == "__main__":
    main()
