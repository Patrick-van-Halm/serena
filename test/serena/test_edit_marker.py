from serena.tools import CopyPathTool, CreateTextFileTool, DeletePathTool, MovePathTool, ReadFileTool, Tool


class TestEditMarker:
    def test_tool_can_edit_method(self):
        """Test that Tool.can_edit() method works correctly"""
        # Non-editing tool should return False
        assert issubclass(ReadFileTool, Tool)
        assert not ReadFileTool.can_edit()

        # Editing tool should return True
        assert issubclass(CreateTextFileTool, Tool)
        assert CreateTextFileTool.can_edit()
        assert DeletePathTool.can_edit()
        assert CopyPathTool.can_edit()
        assert MovePathTool.can_edit()
