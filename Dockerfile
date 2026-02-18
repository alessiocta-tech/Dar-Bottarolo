<?php
header('Content-Type: application/json; charset=utf-8');

$host = "localhost";
$db   = "dbiid6aizvpaqr";
$user = "uycfo1ohpkein";
$pass = "PASSWORD_NUOVA";

$conn = new mysqli($host, $user, $pass, $db);

if ($conn->connect_error) {
  http_response_code(500);
  echo json_encode(["ok"=>false,"error"=>$conn->connect_error]);
  exit;
}

echo json_encode(["ok"=>true,"db"=>"connected"]);
$conn->close();
