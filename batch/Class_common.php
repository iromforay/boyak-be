<?php

class Common{

    private $link;
    private $status;
    public function __construct($link){
        $this->link = $link;
    }

    public function setKey($key,$val){
        $idx = $this->getIdx($key);

        if(isset($idx) and $idx>0){ //update
            $this->update_key($idx,$val);
        }else{ // insert
            $this->insert_key($key,$val);
        }
        return $this;
    }

    public function getKey($key){
        $sql = "SELECT `val` FROM `common` WHERE `key` = '".$key."' LIMIT 1";
        echo $sql."\n";
        $result = $this->link->query($sql);
        $row = $result->fetch_object();
        return isset($row->val)? $row->val : "";
    }

    public function getIdx($key){
        $sql = "SELECT `idx` FROM `common` WHERE `key` = '".$key."' LIMIT 1";
        echo $sql."\n";
        $result = $this->link->query($sql);
        $row = $result->fetch_object();
        return $row->idx;
    }

    private  function insert_key($key,$val)
    {
        $query = "INSERT INTO `common` SET `key`='%s', `val`='%s'";
        $sql = sprintf($query,$key,$val );
        echo $sql."\n";
        $this->link->query($sql);
        return $this;
    }
    private function update_key($idx,$val)
    {
        $query = "UPDATE `common` SET `val`='%s' WHERE idx = %d";
        $sql = sprintf($query,$val,$idx );
        echo $sql."\n";
        $this->link->query($sql);
        return $this;
    }

}